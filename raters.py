"""functions for rating tracks"""

import json
import logging
from datetime import timezone as tz, datetime as dt
from tortoise.functions import Sum
from tortoise.transactions import in_transaction
from humanize import naturaltime
from models import Rating, PlayHistory, WebTrack
from spot_funcs import trackinfo, normalizetrack
from helpers import feelabout
from socket_funcs import queue_webuser_updates

PLAYHISTORY = """
    SELECT 
        p.track_id,
        t.trackname,
        MAX(p.played_at) as played_at,
        array_agg(distinct u.displayname) as listeners
    FROM 
        playhistory p
    JOIN 
        public.user u ON p.user_id = u.id
    JOIN
        track t ON p.track_id = t.id
    GROUP BY 
        p.track_id, t.trackname
    ORDER BY 
        MAX(p.played_at) DESC
    LIMIT 20
"""

async def rate(user, track,
               value=1, 
               last_played=dt.now(tz.utc),
               downrate=False):
    """rate a track, don't downrate unless forced"""
    procname="rate"
    
    track = await normalizetrack(track)
    
    logging.info("%s writing a rating: %s %s %s", 
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
        if int(rating.rating) > int(value) and downrate is False:
            logging.info("%s won't auto-downrate %s from %s to %s for user %s", 
                         procname, track.trackname, rating.rating, value, user.displayname)
        else:
            logging.debug("%s writing a rating: %s %s %s",
                          procname, user.displayname, track.trackname, value)
            rating.rating = int(value)
            await rating.save()

    return rating


async def quickrate(user, message):
    """
    Quick rate a track based on a message and a user object.
    """
    logging.info("quickrate %s %s", user.displayname, message)
    message = json.loads(message)
    
    if 'quickrate' not in message:
        logging.error("no quickrate tasks in this message: %s", message)
        return
    
    track_id = message['quickrate']['track_id']
    html_id = message['quickrate']['html_id']
    change = message['quickrate']['change']
    
    rating = await get_rating(user, track_id)
    
    value = rating.rating + change
    if value < -4 or value > 4:
        logging.error("Invalid rating value %s for track %s by user %s", value, 
                          rating.trackname, user.displayname)
        return
    
    logging.info("quickrate - %s from (%s) to (%s) for track: %s", 
                    user.displayname, rating.rating, value, rating.trackname)
    rating.rating = value
    rating.last_played = dt.now(tz.utc)
    await rating.save()
    
    
    logging.debug("Quick rate successful for track %s by user %s", 
                        rating.trackname, user.displayname)
    try:
        # update the trackname's color to reflect the new rating
        updates = {
            "update": [
            {
                "id": html_id,
                "attribute": "class",
                "value": "track-name " + feelabout(value)
            }
            ]
        }
        await queue_webuser_updates(user.id, updates)
    except Exception as e:
        logging.error("Error sending update to user %s: %s", user.displayname, e)


async def get_rating(user, track_id) -> int:
    """get a track's rating, create a new one if necessary"""
    procname="get_rating"

    track = await normalizetrack(track_id)
    
    logging.debug("%s get_or_creating a rating: %s %s", procname, user.displayname, track.trackname)
    
    # fetch it or create it if it didn't already exist
    rating, _ = await Rating.get_or_create(user_id=user.id,
                                                track_id=track.id,
                                                trackname=track.trackname,
                                                defaults={
                                                   "rating": 0,
                                                   "last_played": dt.now(tz.utc)
                                                   }
                                               )
    return rating  


async def record(state, user_id=None):
    """write a record to the play history table"""
    logging.debug("recording play history %s %s", state.user.displayname, state.t())
    
    if state.recorded:
        logging.warning("state says recorded already, not re-recording")
        return state.rating
    
    state.recorded = True
    
    if not user_id:
        user_id = state.user.id
    
    try:
        history = await PlayHistory.create(
            track_id=state.track.id,
            trackname=state.track.trackname,
            user_id=user_id,
            rating_id=state.rating.id
        )
    except Exception as e:
        logging.error("record exception creating playhistory\n%s", e)
        history = None  # Ensure history is defined even in case of an exception
    
    return history


async def rate_history(spotify, user, token, value=1, limit=20):
    """pull recently played tracks and write ratings for them"""
    with spotify.token_as(token):
        rp = await spotify.playback_recently_played(limit=limit)
        # rp = await spotify.all_items()
    for each in rp.items:
        track = await trackinfo(spotify, each.track.id)
        rating = await Rating.get_or_none(track_id=track.id, user_id=user.id)
        if rating:
            logging.info("won't re-rate a history item that has already been rated")
        else:
            await rate(user, track, value=value, last_played=each.played_at)


async def rate_saved(spotify, user, token, value=4,
                     last_played=dt(1970, 1, 1)):
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
    
    # there is a better way to do this but I haven't found it yet
    if users is not None:
        return await (Rating.filter(id=track.id)
                            .filter(user_id__in=[x.id for x in users])
                            .prefetch_related("user"))
    
    return await (Rating.filter(track_id=track.id)
                        .prefetch_related("user"))


async def get_user_ratings(user, tracks):
    """pull ratings for a list of tracks for a given user"""
    ratings: list = []
    tids = [x.id for x in tracks]
    ratings = await ( Rating.filter(user_id=user.id)
                            .filter(track_id__in=tids)
                            .prefetch_related("track")
                            .prefetch_related('user'))
    
    return ratings


async def get_recent_playhistory_with_ratings(user_id: int):
    """Query for the most recent play history records and include the ratings for a user"""

    results = []
    # Main query to get recent play history with user displaynames
    async with in_transaction() as connection:
        _, recent_playhistory = await connection.execute_query(PLAYHISTORY)

    for playhistory in recent_playhistory:

        rating = await Rating.filter(user_id=user_id, track_id=playhistory['track_id']).first()
        
        webtrack = WebTrack(
            trackname=playhistory['trackname'],
            track_id=playhistory['track_id'],
            color=feelabout(rating.rating if rating else None),
            rating=rating.rating if rating else None,
            timestamp=naturaltime(playhistory['played_at']),
            listeners=playhistory['listeners']
        )

        results.append(webtrack)

    return results


async def rate_by_position(user, last_track, last_position, value=1):
    """set the rating for a track based on the last position when we last saw it"""
    if last_position <=33:
        value = -2
    elif last_position <=80:
        value = -1
    
    await rate(user, last_track, value, downrate=True)
    logging.info("%s track change detected, position autorate %d%% %s %s",
                    __name__, last_position, value, last_track.trackname)
