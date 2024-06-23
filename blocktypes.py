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

async def recently_rated_tracks(days=7):
    """fetch tracks that have been rated in the last few days"""
    interval = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    ratings = await Rating.filter(last_played__gte=interval)
    return ratings


async def popular_tracks(count=1, rating=0):
    """recommendation - fetch a number of positively rated tracks that haven't been played recently
    
    """
    procname = "popular_tracks"
    recent_tracks = await recently_rated_tracks()
    recent_tids = [x.id for x in recent_tracks]
    
    activeusers = await getactiveusers()
    active_uids = [x.id for x in activeusers]

    logging.debug("%s pulled %s recently played tracks",
                 procname, len(recent_tids))

    tids = ( await Rating.annotate(sum=Sum("rating"))
                        .annotate(order=Random())
                        .group_by('track_id')
                        .filter(sum__gte=rating)
                        .filter(user_id__in=active_uids)
                        .exclude(track_id__in=recent_tids)
                        .order_by('order')
                        .limit(count)
                        .values_list("track_id", flat=True))
    
    logging.debug("%s pulled %s results", procname, len(tids))

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
        tids = tids[0].id
    
    return tids
