"""functions for rating tracks"""

import logging
import datetime
from tortoise.functions import Sum
from models import Rating, PlayHistory
from spot_funcs import trackinfo, normalizetrack
from helpers import truncate_middle

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace, trailing-newlines

async def rate(user, track,
               value=1, 
               last_played=datetime.datetime.now(datetime.timezone.utc),
               downrate=False):
    """rate a track, don't downrate unless forced"""
    procname="rate"
    
    track = await normalizetrack(track)
    
    logging.debug("%s writing a rating: %s %s %s", 
                 procname, user.displayname, track.trackname, value)

    # fetch it or create it if it didn't already exist
    rating, created = await Rating.get_or_create(user_id=user.id,
                                                track_id=track.id,
                                                defaults={
                                                   "rating": value,
                                                   "trackname": track.trackname,
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
                         procname, track.trackname, rating.rating, value, user.displayname)
        else:
            logging.debug("%s writing a rating: %s %s %s",
                          procname, user.displayname, track.trackname, value)
            rating.rating = value
            await rating.save()


async def record(user, track):
    """write a record to the play history table"""
    logging.debug("recording play history %s %s",
                  user.displayname, truncate_middle(track.trackname))
    try:
        insertedkey = await PlayHistory.create(track_id=track.id, trackname=track.trackname)
        await insertedkey.save()
    except Exception as e:
        logging.error("record exception creating playhistory\n%s", e)


async def rate_history(spotify, user, token, value=1, limit=20):
    """pull recently played tracks and write ratings for them"""
    with spotify.token_as(token):
        rp = await spotify.playback_recently_played(limit=limit)
        # rp = await spotify.all_items()
    for each in rp.items:
        track = await trackinfo(spotify, each.track.id)
        await rate(user, track, value=value, last_played=each.played_at)


async def rate_saved(spotify, user, token, value=4,
                     last_played=datetime.datetime(1970, 1, 1)):
    """pull user's saved tracks and write ratings for them"""
    with spotify.token_as(token):
        pages = await spotify.saved_tracks()
        all_items = [each async for each in spotify.all_items(pages)]
        for each in all_items:
            track = await trackinfo(spotify, each.track.id)
            await rate(user, track, value=value, last_played=last_played)


async def get_current_rating(track, activeusers=None):
    """pull the total ratings for a track, optionally for a list of users"""

    # there is a better way to do this but I haven't found it yet
    if activeusers is not None:
        selector = ( Rating.get_or_none()
                         .annotate(sum=Sum("rating"))
                         .filter(id=track.id)
                         .filter(userid__in=activeusers)
                         .group_by('track_id')
                         .values_list("sum", flat=True))

    else:
        selector = ( Rating.get_or_none()
                         .annotate(sum=Sum("rating"))
                         .filter(id=track.id)
                         .group_by('track_id')
                         .values_list("sum", flat=True))
        
    return await selector


async def get_track_ratings(track, users=None):
    """pull all ratings for a track, optionally limited to a set of users"""

    selector = Rating.filter(id=track.id).prefetch_related("user")
    
    # there is a better way to do this but I haven't found it yet
    if users is not None:
        selector = selector.filter(userid__in=[x.id for x in users])
        
    return await selector


async def get_user_ratings(user, tracks):
    """pull ratings for a list of tracks for a given user"""
    ratings: list = []
    tids = [x.id for x in tracks]
    ratings = await ( Rating.filter(user_id=user.id)
                        .filter(track_id__in=tids)
                        .prefetch_related("track")
                        .prefetch_related('user'))
    
    return ratings
