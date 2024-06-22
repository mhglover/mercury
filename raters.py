"""functions for rating tracks"""

import logging
import datetime
from tortoise.functions import Sum
from queue_manager import trackinfo
from models import Rating, PlayHistory

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace


async def rate(spotify, uid, tid,
               value=1, 
               last_played=datetime.datetime.now(datetime.timezone.utc),
               downrate=False):
    """rate a track, don't downrate unless forced"""
    procname="rate"
    
    # make sure this is in the database and get the name for logging
    try:
        trackname = await trackinfo(spotify, tid)
    except Exception as e: # pylint: disable=broad-exception-caught
        logging.info("rate exception adding a track to database: [%s]\n%s",
                     tid, e)

    logging.info("%s writing a rating: %s %s %s", procname, uid, trackname, value)

    # fetch it or create it if it didn't already exist
    rating, created = await Rating.get_or_create(userid=uid,
                                                trackid=tid,
                                                defaults={
                                                   "rating": value,
                                                   "trackname": trackname,
                                                   "last_played": last_played
                                                   }
                                               )

    # if the rating already existed, update it
    if not created:
        
        # always update the last_played time
        rating.last_played = last_played
        await rating.save()
        
        # don't automatically downrate
        if rating.rating > value and downrate is False:
            logging.info("%s won't auto-downrate %s from %s to %s for user %s", 
                         procname, trackname, rating.rating, value, uid)
        else:
            logging.debug("%s writing a rating: %s %s %s",
                          procname, uid, trackname, value)
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


async def rate_history(spotify, user, token, value=1, count=20):
    """pull recently played tracks and write ratings for them"""
    with spotify.token_as(token):
        rp = await spotify.playback_recently_played(limit=count)
        # rp = await spotify.all_items()
    for each in rp.items:
        await rate(spotify, user.spotifyid, each.track.id,
                    value=value, last_played=each.played_at)
    

async def get_current_rating(trackid, activeusers=None):
    """pull the total ratings for a track, optionally for a list of users"""

    # there is a better way to do this but I haven't found it yet
    if activeusers is not None:
        selector = ( Rating.get_or_none()
                         .annotate(sum=Sum("rating"))
                         .filter(trackid=trackid)
                         .group_by('trackid')
                         .values_list("sum", flat=True))

    else:
        selector = ( Rating.get_or_none()
                         .annotate(sum=Sum("rating"))
                         .filter(trackid=trackid)
                         .filter(userid__in=activeusers)
                         .group_by('trackid')
                         .values_list("sum", flat=True))
        
    return await selector
