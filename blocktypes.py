"""functions for pulling tracks for recommendations"""
import datetime
import logging
from tortoise.functions import Sum
from tortoise.expressions import Subquery, F
from tortoise.contrib.postgres.functions import Random
from models import Rating, PlayHistory, Track
from users import getactiveusers
from spot_funcs import trackinfo

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace, trailing-newlines

# each recommendation function should return a single track object by default
# all functions should return either a single track object, a list of track objects,
# or an empty list

async def recently_rated_tracks(days=7):
    """fetch tracks that have been rated in the last few days"""
    interval = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    ratings = await Rating.filter(last_played__gte=interval).prefetch_related("track")
    tracks = [x.track for x in ratings]
    return tracks


async def popular_tracks(count=1, rating=0):
    """recommendation - fetch a number of rated tracks that haven't been played recently
    excludes played in the last 1 day
    excludes duration less than 1 minute or more than 7 minutes
    
    returns either one or a list of rating objects
    """
    procname = "popular_tracks"
    recent_tracks = await recently_rated_tracks(days=1)
    recent_tids = [x.id for x in recent_tracks]
    
    activeusers = await getactiveusers()
    active_uids = [x.id for x in activeusers]

    logging.debug("%s pulled %s recently played tracks",
                 procname, len(recent_tids))

    ratings = ( await Rating.annotate(sum=Sum("rating"))
                        .annotate(order=Random())
                        .group_by('track_id')
                        .filter(sum__gte=rating)
                        .filter(user_id__in=active_uids)
                        .exclude(track_id__in=recent_tids)
                        .exclude(track__duration_ms__lte=60000)
                        .exclude(track__duration_ms__gte=420000)
                        .order_by('order')
                        .limit(count)
                        .prefetch_related("track"))
        
    logging.debug("%s pulled %s results", procname, len(ratings))
    
    tracks = [x.track for x in ratings]

    # return an empty list
    if len(tracks) == 0:
        logging.warning("%s no potential tracks to queue", procname)
        tracks = []
    
    # if there's just one, don't return a list
    if len(tracks) == 1:
        tracks = tracks[0]
    
    return tracks


async def spotrec_tracks(spotify, count=1):
    """recommendation - fetch a number of spotify recommendations for a specific user
    
    takes:
        spotify: spotify connection object
        token: spotify user token for suggestion
        seeds: a list of tracks
    """
    procname = "spotrec_tracks"
    
    # get the last five PlayHistory tracks for seeds
    seed_tracks = await PlayHistory.filter().order_by('-id').limit(5).prefetch_related('track')
    
    seed_spotifyids = [x.track.spotifyid for x in seed_tracks]

    logging.debug("%s getting spotify recommendations", procname)
    utrack = await spotify.recommendations(track_ids=seed_spotifyids, limit=count)
    
    tracks = [await trackinfo(spotify, x.id) for x in utrack.tracks]
        
    if len(tracks) == 0:
        logging.warning("%s no potential tracks to queue", procname)
        tracks = []
    
    if len(tracks) == 1:
        tracks = tracks[0]
    
    return tracks


async def get_fresh_tracks(count=1):
    """get a list of tracks that haven't been rated by the current listeners"""
    user_ids = [user.id for user in await getactiveusers()]
    subquery = Rating.filter(user_id__in=user_ids).values('track_id')
    tracks = await ( Track.annotate(order=Random())
                          .exclude(id__in=Subquery(subquery))
                          .exclude(duration_ms__lte=60000)
                          .exclude(duration_ms__gte=420000)
                          .order_by('order')
                          .limit(count))

    if len(tracks) == 1:
        return tracks[0]
    return tracks
