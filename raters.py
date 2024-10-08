"""functions for rating tracks"""

import logging
from datetime import timezone as tz, datetime as dt, timedelta as td
from tortoise.functions import Sum
from tortoise.transactions import in_transaction
from humanize import naturaltime
from models import Rating, PlayHistory, WebTrack
from spot_funcs import trackinfo, normalizetrack
from helpers import feelabout

PLAYHISTORY = """
    SELECT 
        p.track_id,
        t.trackname,
        MAX(p.played_at) as played_at,
        array_agg(distinct u.displayname) as listeners,
		max(p.reason) as reason
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
        if int(rating.rating) > int(value) and downrate is False:
            logging.info("%s won't auto-downrate %s from %s to %s for user %s", 
                         procname, track.trackname, rating.rating, value, user.displayname)
        else:
            logging.debug("%s writing a rating: %s %s %s",
                          procname, user.displayname, track.trackname, value)
            rating.rating = int(value)
            await rating.save()

    return rating


async def get_rating(user, track_id) -> int:
    """get a track's rating, create a new one if necessary"""
    procname="get_rating"

    track = await normalizetrack(track_id)
    
    logging.debug("%s get_or_creating a rating: %s %s", procname, user.displayname, track.trackname)
    
    # fetch it or create it if it didn't already exist
    rating, _ = await Rating.get_or_create( user_id=user.id,
                                            track_id=track.id,
                                            defaults={
                                                "trackname": track.trackname,
                                                "rating": 0,
                                                "last_played": dt.now(tz.utc)
                                                }
                                            )
    return rating  


async def record_history(user, track, reason: str):
    """write a record to the play history table"""
    logging.debug("recording play history %s %s", user.displayname, track.trackname, reason)
    
    # check for a recent record of this track (within 15 minutes)
    interval = dt.now(tz.utc) - td(minutes=15)
    recent = await (PlayHistory.filter(track_id=track.id,
                                       user_id=user.id,
                                       played_at__gte=interval)
                               .prefetch_related("user")
                               .first()
                    )
    if recent:
        logging.info("record_history %s already wrote history: %s (%s)", user.displayname, track.trackname, reason)
        
        return recent
    
    # if there's a rating, get it to use in the history record
    rating = await Rating.get_or_none(track_id=track.id, user_id=user.id)
    
    # since we didn't find a history, let's make one
    try:
        history = await PlayHistory.create(
            track_id=track.id,
            trackname=track.trackname,
            user_id=user.id,
            rating_id=rating.id if rating else None,
            reason=reason
        )
    except Exception as e:
        logging.error("record exception creating playhistory\n%s", e)
        history = None  # Ensure history is defined even in case of an exception
    
    logging.info("record_history %s wrote history: %s (%s)", user.displayname, track.trackname, reason)
    return history


async def rate_history(spotify, user, token, value=1, limit=50):
    """pull recently played tracks and write ratings for them"""
    with spotify.token_as(token):
        rp = await spotify.playback_recently_played(limit=limit)
    for each in rp.items:
        track = await trackinfo(spotify, spotifyid=each.track.id, token=token)
        rating = await Rating.get_or_none(track_id=track.id, user_id=user.id)
        if rating:
            logging.debug("rate_history already exists for %s, %s", user.displayname, track.trackname)
            
        else:
            logging.info("rate_history - %s %s %s", user.displayname, track.trackname, value)
            try:
                await rate(user, track, value=value, last_played=each.played_at)
            except Exception as e:
                logging.error("rate_history exception rating %s\n%s", track.trackname, e)
    logging.info("rate_history finished for %s", user.displayname)


async def rate_saved(spotify, user, token, value=4,
                     last_played=dt(1970, 1, 1)):
    """pull user's saved tracks and write ratings for them"""
    with spotify.token_as(token):
        pages = await spotify.saved_tracks()
        all_items = [each async for each in spotify.all_items(pages)]
        for each in all_items:
            track = await trackinfo(spotify, spotifyid=each.track.id, token=token)
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
            listeners=playhistory['listeners'],
            reason=playhistory['reason']
        )

        results.append(webtrack)

    return results


async def rate_by_position(user, last_track, last_position):
    """set the rating for a track based on the last position when we last saw it"""
    if last_position == 0:
        logging.warning("rate_by_position trackchange at 0%%, won't rate at all %s %s %s", user.displayname, last_track.trackname, last_position)
        return
    if last_position <=15:
        value = -2
    elif last_position <=40:
        value = -1
    else:
        value = 0
    
    await rate(user, last_track, value, downrate=True)
    logging.info("%s track change detected, position autorate %d%% %s %s",
                    __name__, last_position, value, last_track.trackname)
