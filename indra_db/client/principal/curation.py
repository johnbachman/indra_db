__all__ = ['submit_curation', 'get_curations', 'get_grounding_curations']

import re
import logging

from sqlalchemy.exc import IntegrityError

from indra_db.util import get_primary_db
from indra_db.exceptions import BadHashError

logger = logging.getLogger(__name__)


def submit_curation(hash_val, tag, curator, ip, text=None,
                    ev_hash=None, source='direct_client', db=None):
    """Submit a curation for a given preassembled or raw extraction.

    Parameters
    ----------
    hash_val : int
        The hash corresponding to the statement.
    tag : str
        A very short phrase categorizing the error or type of curation.
    curator : str
        The name or identifier for the curator.
    ip : str
        The ip address of user's computer.
    text : str
        A brief description of the problem.
    ev_hash : int
        A hash of the sentence and other evidence information. Elsewhere
        referred to as `source_hash`.
    source : str
        The name of the access point through which the curation was performed.
        The default is 'direct_client', meaning this function was used
        directly. Any higher-level application should identify itself here.
    db : DatabaseManager
        A database manager object used to access the database.
    """
    if db is None:
        db = get_primary_db()

    inp = {'tag': tag, 'text': text, 'curator': curator, 'ip': ip,
           'source': source, 'pa_hash': hash_val, 'source_hash': ev_hash}

    logger.info("Adding curation: %s" % str(inp))

    try:
        dbid = db.insert(db.Curation, **inp)
    except IntegrityError as e:
        logger.error("Got a bad entry.")
        msg = e.args[0]
        detail_line = msg.splitlines()[1]
        m = re.match("DETAIL: .*?\(pa_hash\)=\((\d+)\).*?not present.*?pa.*?",
                     detail_line)
        if m is None:
            raise e
        else:
            h = m.groups()[0]
            assert int(h) == int(hash_val), \
                "Erred hash %s does not match input hash %s." % (h, hash_val)
            logger.error("Bad hash: %s" % h)
            raise BadHashError(h)
    return dbid


def get_curations(db=None, **params):
    """Get all curations for a certain level given certain criteria."""
    if db is None:
        db = get_primary_db()
    cur = db.Curation

    constraints = []
    for key, val in params.items():
        if key == 'hash_val':
            key = 'pa_hash'
        if key == 'ev_hash':
            key = 'source_hash'
        if isinstance(val, list) or isinstance(val, set) \
           or isinstance(val, tuple):
            constraints.append(getattr(cur, key).in_(val))
        else:
            constraints.append(getattr(cur, key) == val)

    return db.select_all(cur, *constraints)


def get_grounding_curations(db=None):
    """Return a dict of curated groundings from a given database.

    Parameters
    ----------
    db : Optional[DatabaseManager]
        A database manager object used to access the database. If not given,
        the database configured as primary is used.

    Returns
    -------
    dict
        A dict whose keys are raw text strings and whose values are dicts of DB
        name space to DB ID mappings corresponding to the curated grounding.
    """
    # Get all the grounding curations
    curs = get_curations(db=db, tag='grounding')
    groundings = {}
    for cur in curs:
        # If there is no curation given, we skip it
        if not cur.text:
            continue
        # We now try to match the standard pattern for grounding curation
        cur_text = cur.text.strip()
        match = re.match('^\[(.*)\] -> ([^ ]+)$', cur_text)
        # We log any instances of curations that don't match the pattern
        if not match:
            logger.info('"%s" by %s does not match the grounding curation '
                        'pattern.' % (cur_text, cur.curator))
            continue
        txt, dbid_str = match.groups()
        # We now get a dict of curated mappings to return
        try:
            dbid_entries = [entry.split(':', maxsplit=1)
                            for entry in dbid_str.split('|')]
            dbids = {k: v for k, v in dbid_entries}
        except Exception as e:
            logger.info('Could not interpret DB IDs: %s for %s' %
                        (dbid_str, txt))
            continue
        if txt in groundings and groundings[txt] != dbids:
            logger.info('There is already a curation for %s: %s, '
                        'overwriting with %s' % (txt, str(groundings[txt]),
                                                 str(dbids)))
        groundings[txt] = dbids
    return groundings