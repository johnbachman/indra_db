__all__ = ['distill_stmts', 'get_filtered_rdg_stmts', 'get_filtered_db_stmts',
           'delete_raw_statements_by_id', 'get_reading_stmt_dict',
           'reader_versions', 'text_content_sources']

import json
import pickle
import logging

from datetime import datetime
from itertools import groupby
from functools import partial
from multiprocessing.pool import Pool

from indra.statements import Statement
from indra.util import batch_iter, clockit
from indra.util.nested_dict import NestedDict

from .helpers import _set_evidence_text_ref

logger = logging.getLogger('util-distill')


def get_reading_stmt_dict(db, clauses=None, get_full_stmts=True):
    """Get a nested dict of statements, keyed by ref, content, and reading."""
    # Construct the query for metadata from the database.
    q = (db.session.query(db.TextRef, db.TextContent.id,
                          db.TextContent.source, db.Reading.id,
                          db.Reading.reader_version, db.RawStatements.id,
                          db.RawStatements.json)
         .filter(db.RawStatements.reading_id == db.Reading.id,
                 db.Reading.text_content_id == db.TextContent.id,
                 db.TextContent.text_ref_id == db.TextRef.id))
    if clauses:
        q = q.filter(*clauses)

    # Prime some counters.
    num_duplicate_evidence = 0
    num_unique_evidence = 0

    # Populate a dict with all the data.
    stmt_nd = NestedDict()
    for tr, tcid, src, rid, rv, sid, sjson in q.yield_per(1000):
        # Back out the reader name.
        for reader, rv_list in reader_versions.items():
            if rv in rv_list:
                break
        else:
            raise Exception("rv %s not recognized." % rv)

        # Get the json for comparison and/or storage
        stmt_json = json.loads(sjson.decode('utf8'))
        stmt = Statement._from_json(stmt_json)
        _set_evidence_text_ref(stmt, tr)

        # Hash the compbined stmt and evidence matches key.
        stmt_hash = stmt.get_hash(shallow=False)

        # For convenience get the endpoint statement dict
        s_dict = stmt_nd[tr.id][src][tcid][reader][rv][rid]

        # Initialize the value to a set, and count duplicates
        if stmt_hash not in s_dict.keys():
            s_dict[stmt_hash] = set()
            num_unique_evidence += 1
        else:
            num_duplicate_evidence += 1

        # Either store the statement, or the statement id.
        if get_full_stmts:
            s_dict[stmt_hash].add((sid, stmt))
        else:
            s_dict[stmt_hash].add((sid, None))

    # Report on the results.
    print("Found %d relevant text refs with statements." % len(stmt_nd))
    print("number of statement exact duplicates: %d" % num_duplicate_evidence)
    print("number of unique statements: %d" % num_unique_evidence)
    return stmt_nd


# Specify versions of readers, and preference. Later in the list is better.
reader_versions = {
    'sparser': ['sept14-linux\n', 'sept14-linux', 'June2018-linux',
                'October2018-linux'],
    'reach': ['61059a-biores-e9ee36', '1.3.3-61059a-biores-']
}

# Specify sources of fulltext content, and order priorities.
text_content_sources = ['pubmed', 'elsevier', 'manuscripts', 'pmc_oa']


def get_filtered_rdg_stmts(stmt_nd, get_full_stmts, linked_sids=None,
                           ignore_duplicates=False):
    """Get the set of statements/ids from readings minus exact duplicates."""
    logger.info("Filtering the statements from reading.")
    if linked_sids is None:
        linked_sids = set()
    def better_func(element):
        return text_content_sources.index(element)

    # Now we filter and get the set of statements/statement ids.
    stmt_tpls = set()
    duplicate_sids = set()  # Statements that are exact duplicates.
    bettered_duplicate_sids = set()  # Statements with "better" alternatives
    for trid, src_dict in stmt_nd.items():
        some_bettered_duplicate_tpls = set()
        # Filter out unneeded fulltext.
        while len(src_dict) > 1:
            try:
                worst_src = min(src_dict, key=better_func)
                some_bettered_duplicate_tpls |= src_dict[worst_src].get_leaves()
                del src_dict[worst_src]
            except:
                print(src_dict)
                raise

        # Filter out the older reader versions
        for reader, rv_list in reader_versions.items():
            for rv_dict in src_dict.gets(reader):
                best_rv = max(rv_dict, key=lambda x: rv_list.index(x))

                # Record the rest of the statement uuids.
                for rv, r_dict in rv_dict.items():
                    if rv != best_rv:
                        some_bettered_duplicate_tpls |= r_dict.get_leaves()

                # Take any one of the duplicates. Statements/Statement ids are
                # already grouped into sets of duplicates keyed by the
                # Statement and Evidence matches key hashes. We only want one
                # of each.
                stmt_set_itr = (stmt_set for r_dict in rv_dict[best_rv].values()
                                for stmt_set in r_dict.values())
                if ignore_duplicates:
                    some_stmt_tpls = {stmt_tpl for stmt_set in stmt_set_itr
                                      for stmt_tpl in stmt_set}
                else:
                    some_stmt_tpls, some_duplicate_tpls = \
                        _detect_exact_duplicates(stmt_set_itr, linked_sids)

                    # Get the sids for the statements.
                    duplicate_sids |= {sid for sid, _ in some_duplicate_tpls}

                stmt_tpls |= some_stmt_tpls

        # Add the bettered duplicates found in this round.
        bettered_duplicate_sids |= \
            {sid for sid, _ in some_bettered_duplicate_tpls}

    if get_full_stmts:
        stmts = {stmt for _, stmt in stmt_tpls if stmt is not None}
        assert len(stmts) == len(stmt_tpls), \
            ("Some statements were None! The interaction between "
             "_get_reading_statement_dict and _filter_rdg_statements was "
             "probably mishandled.")
    else:
        stmts = {sid for sid, _ in stmt_tpls}

    return stmts, duplicate_sids, bettered_duplicate_sids


def _detect_exact_duplicates(stmt_set_itr, linked_sids):
    # Pick one among any exact duplicates. Unlike with bettered
    # duplicates, these choices are arbitrary, and such duplicates
    # can be deleted.
    stmt_tpls = set()
    some_duplicate_tpls = set()
    for stmt_tpl_set in stmt_set_itr:
        if not stmt_tpl_set:
            continue
        elif len(stmt_tpl_set) == 1:
            # There isn't really a choice here.
            stmt_tpls |= stmt_tpl_set
        else:
            prefed_tpls = {tpl for tpl in stmt_tpl_set
                           if tpl[0] in linked_sids}
            if not prefed_tpls:
                # Pick the first one to pop, record the rest as
                # duplicates.
                stmt_tpls.add(stmt_tpl_set.pop())
                some_duplicate_tpls |= stmt_tpl_set
            elif len(prefed_tpls) == 1:
                # There is now no choice: just take the preferred
                # statement.
                stmt_tpls |= prefed_tpls
                some_duplicate_tpls |= (stmt_tpl_set - prefed_tpls)
            else:
                # This shouldn't happen, so an early run of this
                # function must have failed somehow, or else there
                # was some kind of misuse. Flag it, pick just one of
                # the preferred statements, and delete any deletable
                # statements.
                assert False, \
                    ("Duplicate deduplicated statements found: %s"
                     % str(prefed_tpls))
    return stmt_tpls, some_duplicate_tpls


def _choose_unique(not_duplicates, get_full_stmts, stmt_tpl_grp):
    """Choose one of the statements from a redundant set."""
    assert stmt_tpl_grp, "This cannot be empty."
    if len(stmt_tpl_grp) == 1:
        s_tpl = stmt_tpl_grp[0]
        duplicate_ids = set()
    else:
        stmt_tpl_set = set(stmt_tpl_grp)
        preferred_tpls = {tpl for tpl in stmt_tpl_set
                          if tpl[1] in not_duplicates}
        if not preferred_tpls:
            s_tpl = stmt_tpl_set.pop()
        elif len(preferred_tpls) == 1:
            s_tpl = preferred_tpls.pop()
        else:  # len(preferred_stmts) > 1
            assert False, \
                ("Duplicate deduplicated statements found: %s"
                 % str(preferred_tpls))
        duplicate_ids = {tpl[1] for tpl in stmt_tpl_set
                         if tpl[1] not in not_duplicates}

    if get_full_stmts:
        stmt_json = json.loads(s_tpl[2].decode('utf-8'))
        ret_stmt = Statement._from_json(stmt_json)
    else:
        ret_stmt = s_tpl[1]
    return ret_stmt, duplicate_ids


def get_filtered_db_stmts(db, get_full_stmts=False, clauses=None,
                          not_duplicates=None, num_procs=1):
    """Get the set of statements/ids from databases minus exact duplicates."""
    if not_duplicates is None:
        not_duplicates = set()

    # Only get the json if it's going to be used. Note that if the use of the
    # get_full_stmts parameter is inconsistent in _choose_unique, this will
    # cause some problems.
    if get_full_stmts:
        tbl_list = [db.RawStatements.mk_hash, db.RawStatements.id,
                    db.RawStatements.json]
    else:
        tbl_list = [db.RawStatements.mk_hash, db.RawStatements.id]

    db_s_q = db.filter_query(tbl_list, db.RawStatements.db_info_id.isnot(None))

    # Add any other criterion specified at higher levels.
    if clauses:
        db_s_q = db_s_q.filter(*clauses)

    # Produce a generator of statement groups.
    db_stmt_data = db_s_q.order_by(db.RawStatements.mk_hash).yield_per(10000)
    choose_unique_stmt = partial(_choose_unique, not_duplicates, get_full_stmts)
    stmt_groups = (list(grp) for _, grp
                   in groupby(db_stmt_data, key=lambda x: x[0]))

    # Actually do the comparison.
    if num_procs is 1:
        stmts = set()
        duplicate_ids = set()
        for stmt_list in stmt_groups:
            stmt, some_duplicates = choose_unique_stmt(stmt_list)
            stmts.add(stmt)
            duplicate_ids |= some_duplicates
    else:
        pool = Pool(num_procs)
        print("Filtering db statements in %d processess." % num_procs)
        res = pool.map(choose_unique_stmt, stmt_groups)
        pool.close()
        pool.join()
        stmt_list, duplicate_sets = zip(*res)
        stmts = set(stmt_list)
        duplicate_ids = {uuid for dup_set in duplicate_sets for uuid in dup_set}

    return stmts, duplicate_ids


@clockit
def distill_stmts(db, get_full_stmts=False, clauses=None, num_procs=1,
                  handle_duplicates='ignore', weed_evidence=True,
                  batch_size=1000):
    """Get a corpus of statements from clauses and filters duplicate evidence.

    Parameters
    ----------
    db : :py:class:`DatabaseManager`
        A database manager instance to access the database.
    get_full_stmts : bool
        By default (False), only Statement ids (the primary index of Statements
        on the database) are returned. However, if set to True, serialized
        INDRA Statements will be returned. Note that this will in general be
        VERY large in memory, and therefore should be used with caution.
    clauses : None or list of sqlalchemy clauses
        By default None. Specify sqlalchemy clauses to reduce the scope of
        statements, e.g. `clauses=[db.Statements.type == 'Phosphorylation']` or
        `clauses=[db.Statements.uuid.in_([<uuids>])]`.
    num_procs : int
        Select the number of process that can be used.
    handle_duplicates : 'ignore', 'delete', or a string file path
        Choose whether you want to delete the statements that are found to be
        duplicates ('delete'), or write a pickle file with their ids (at the
        string file path) for later handling, or simply do nothing ('ignore').
        The default behavior is 'ignore'.
    weed_evidence : bool
        If True, evidence links that exist for raw statements that now have
        better alternatives will be removed. If False, such links will remain,
        which may cause problems in incremental pre-assembly.

    Returns
    -------
    stmt_ret : set
        A set of either statement ids or serialized statements, depending on
        `get_full_stmts`.
    """
    if handle_duplicates == 'delete' or handle_duplicates != 'ignore':
        logger.info("Looking for ids from existing links...")
        linked_sids = {sid for sid,
                       in db.select_all(db.RawUniqueLinks.raw_stmt_id)}
    else:
        linked_sids = set()

    # Get de-duplicated Statements, and duplicate uuids, as well as uuid of
    # Statements that have been improved upon...
    logger.info("Sorting reading statements...")
    stmt_nd = get_reading_stmt_dict(db, clauses, get_full_stmts)

    stmts, duplicate_sids, bettered_duplicate_sids = \
        get_filtered_rdg_stmts(stmt_nd, get_full_stmts, linked_sids)
    logger.info("After filtering reading: %d unique statements, %d exact "
                "duplicates, %d with results from better resources available."
                % (len(stmts), len(duplicate_sids),
                   len(bettered_duplicate_sids)))
    assert not linked_sids & duplicate_sids, linked_sids & duplicate_sids
    del stmt_nd  # This takes up a lot of memory, and is done being used.

    db_stmts, db_duplicates = \
        get_filtered_db_stmts(db, get_full_stmts, clauses, linked_sids,
                              num_procs)
    stmts |= db_stmts
    duplicate_sids |= db_duplicates
    logger.info("After filtering database statements: %d unique, %d duplicates"
                % (len(stmts), len(duplicate_sids)))
    assert not linked_sids & duplicate_sids, linked_sids & duplicate_sids

    # Remove support links for statements that have better versions available.
    bad_link_sids = bettered_duplicate_sids & linked_sids
    if len(bad_link_sids) and weed_evidence:
        logger.info("Removing bettered evidence links...")
        rm_links = db.select_all(
            db.RawUniqueLinks,
            db.RawUniqueLinks.raw_stmt_id.in_(bad_link_sids)
        )
        db.delete_all(rm_links)

    # Delete exact duplicates
    if len(duplicate_sids):
        if handle_duplicates == 'delete':
            logger.info("Deleting duplicates...")
            for dup_id_batch in batch_iter(duplicate_sids, batch_size, set):
                logger.info("Deleting %d duplicated raw statements."
                            % len(dup_id_batch))
                delete_raw_statements_by_id(db, dup_id_batch)
        elif handle_duplicates != 'ignore':
            with open('duplicate_ids_%s.pkl' % datetime.now(), 'wb') as f:
                pickle.dump(duplicate_sids, f)

    return stmts


def delete_raw_statements_by_id(db, raw_sids, sync_session=False,
                                remove='all'):
    """Delete raw statements, their agents, and their raw-unique links.

    It is best to batch over this function with sets of 1000 or so ids. Setting
    sync_session to False will result in a much faster resolution, but you may
    find some ORM objects have not been updated.
    """
    if remove == 'all':
        remove = ['links', 'agents', 'statements']

    # First, delete the evidence links.
    if 'links' in remove:
        ev_q = db.filter_query(db.RawUniqueLinks,
                               db.RawUniqueLinks.raw_stmt_id.in_(raw_sids))
        logger.info("Deleting any connected evidence links...")
        ev_q.delete(synchronize_session=sync_session)

    # Second, delete the agents.
    if 'agents' in remove:
        ag_q = db.filter_query(db.RawAgents,
                               db.RawAgents.stmt_id.in_(raw_sids))
        logger.info("Deleting all connected agents...")
        ag_q.delete(synchronize_session=sync_session)

    # Now finally delete the statements.
    if 'statements' in remove:
        raw_q = db.filter_query(db.RawStatements,
                                db.RawStatements.id.in_(raw_sids))
        logger.info("Deleting all raw indicated statements...")
        raw_q.delete(synchronize_session=sync_session)
    return