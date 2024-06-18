"""functions for rating tracks"""

import logging
import datetime
from queue_manager import trackinfo
from models import Rating, PlayHistory

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

async def rate_list(items, uid, rating=1, set_last_played=True):
    """rate a bunch of stuff at once"""
    if isinstance(items, list):
        if isinstance(items[0], str):
            trackids = items
        else:
            trackids = [x.id for x in items]
    else:
        trackids = [x.track.id for x in items]
    logging.info("rating %s tracks", len(trackids))

    for tid in trackids:
        await rate(uid, tid, rating, set_last_played=set_last_played)

    return len(trackids)


async def rate(spotify, uid, tid, value=1, set_last_played=True, autorate=False):
    """rate a track"""
    procname="rate"
    try:
        displayname, track = await trackinfo(spotify, tid, return_track=True)
    except Exception as e: # pylint: disable=broad-exception-caught
        logging.info("rate exception adding a track to database: [%s]\n%s",
                     tid, e)

    logging.info("%s writing a rating: %s %s %s", procname, uid, displayname, value)
    if set_last_played:
        lastplayed = datetime.datetime.now()
    else:
        lastplayed = "1970-01-01"
    rating, created = await Rating.get_or_create(userid=uid,
                                                trackid=tid,
                                                defaults={
                                                   "rating": value,
                                                   "trackname": track.trackname,
                                                   "last_played": lastplayed
                                                   }
                                               )

    # if the rating already existed, update the value and lastplayed time
    if not created:
        if rating.rating > value and autorate is True:
            logging.info("%s won't auto-downrate %s from %s to %s for user %s", 
                         procname, displayname, rating.rating, value, uid)
        else:
            logging.debug("%s writing a rating: %s %s %s",
                          procname, uid, displayname, value)
            rating.rating = value
    
    await rating.save()


async def record(spotify, uid, tid):
    """write a record to the play history table"""
    procname = "record"
    trackname = await trackinfo(spotify, tid)
    logging.info("%s play history %s %s", procname, uid, trackname)
    try:
        insertedkey = await PlayHistory.create(trackid=tid)
        await insertedkey.save()
    except Exception as e:
        logging.error("record exception creating playhistory: %s\n%s",
                      uid, e)
    
    logging.debug("record inserted play history record %s", insertedkey)