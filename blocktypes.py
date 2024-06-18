"""functions for pulling tracks for recommendations"""
import datetime
import logging
from tortoise.functions import Sum
from tortoise.contrib.postgres.functions import Random
from models import Rating
from users import getactiveusers

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

# each recommendation function should return a single track id by default
# all functions should return either a single track id, a list of track ids,
# or an empty list

async def recently_played_tracks(days=5):
    """fetch tracks that have been rated in the last 5 days"""
    interval = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    tids = await Rating.filter(last_played__gte=interval).values_list('trackid', flat=True)
    return tids


async def popular_tracks(count=1, rating=0):
    """recommendation - fetch a number of positively rated tracks that haven't been played recently
    
    """
    procname = "popular_tracks"
    recent_tids = await recently_played_tracks()
    activeusers = await getactiveusers()

    logging.info("%s pulled %s recently played tracks",
                 procname, len(recent_tids))

    tids = ( await Rating.annotate(sum=Sum("rating"))
                        .annotate(order=Random())
                        .group_by('trackid')
                        .filter(sum__gte=rating)
                        .filter(userid__in=activeusers)
                        .exclude(trackid__in=recent_tids)
                        .order_by('order')
                        .limit(count)
                        .values_list("trackid", flat=True))
    
    logging.info("%s pulled %s results", procname, len(tids))

    if len(tids) == 0:
        logging.warning("%s no potential tracks to queue", procname)
        tids = []
    
    if len(tids) == 1:
        tids = tids[0]
    
    return tids


async def spotrec_tracks(spotify, token, trackids, count=1):
    """recommendation - fetch a number of spotify recommendations for a specific user
    """
    procname = "popular_tracks"

    logging.info("%s getting spotify recommendations", procname)
    with spotify.token_as(token):
        utrack = await spotify.recommendations(track_ids=trackids, limit=count)
    
    tids = [x for x in utrack.tracks]
        
    if len(tids) == 0:
        logging.warning("%s no potential tracks to queue", procname)
        tids = []
    
    if len(tids) == 1:
        tids = tids[0]
    
    return tids
