"""functions for pulling tracks for recommendations"""
import datetime
import logging
import pickle
from random import choice
from tortoise.functions import Sum
from tortoise.contrib.postgres.functions import Random
from models import Rating
from users import getactiveusers
from spot_funcs import trackinfo

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

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
    
    returns either one or a list of rating objects
    """
    procname = "popular_tracks"
    recent_tracks = await recently_rated_tracks()
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


async def spotrec_tracks(spotify, activeusers, count=1):
    """recommendation - fetch a number of spotify recommendations for a specific user
    
    takes:
        spotify: spotify connection object
        token: spotify user token for suggestion
        seeds: a list of tracks
    """
    procname = "spotrec_tracks"
    
    rando = choice(activeusers)
    token = pickle.loads(rando.token)
                
    logging.debug("%s queuing a spotify recommendation", procname)
    seeds = await popular_tracks(5)
    
    seed_spotifyids = [x.spotifyid for x in seeds]

    logging.info("%s getting spotify recommendations", procname)
    with spotify.token_as(token):
        utrack = await spotify.recommendations(track_ids=seed_spotifyids, limit=count)
    
    tracks = [await trackinfo(spotify, x.id) for x in utrack.tracks]
        
    if len(tracks) == 0:
        logging.warning("%s no potential tracks to queue", procname)
        tracks = []
    
    if len(tracks) == 1:
        tracks = tracks[0]
    
    return tracks
