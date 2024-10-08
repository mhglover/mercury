"""functions for pulling tracks for recommendations"""
from datetime import timezone as tz, datetime as dt, timedelta
import logging
import asyncio
from random import choice
from tortoise.functions import Sum
from tortoise.expressions import Subquery
from tortoise.contrib.postgres.functions import Random
import tekore as tk
from models import Rating, PlayHistory, Track, Option
from users import getactiveusers, getuser
from spot_funcs import trackinfo

# each recommendation function should return a single track object by default
# all functions should return either a single track object, a list of track objects,
# or an empty list

async def recently_rated_tracks(days=7):
    """fetch tracks that have been rated in the last few days"""
    interval = dt.now(tz.utc) - timedelta(days=days)
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
    track_repeat_timeout, _ = await Option.get_or_create(
                                                    option_name="track_repeat_timeout", 
                                                    defaults = { "option_value": 5 })
    
    # recent_tracks = await recently_rated_tracks(days=1)
    users = await getactiveusers()
    active_uids = [x.id for x in users]
    active_displaynames = [x.displayname for x in users]
    interval = dt.now() - timedelta(days=int(track_repeat_timeout.option_value))
    recent_tids = await (PlayHistory.filter(played_at__gte=interval)
                                    .filter(user_id__in=active_uids)
                                    .values_list("track_id", flat=True))
    
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
    reason = "popular tracks for %s" % ", ".join(active_displaynames)

    # return an empty list
    if len(tracks) == 0:
        logging.warning("%s no potential tracks to queue", procname)
        tracks = []
    
    # if there's just one, don't return a list
    if len(tracks) == 1:
        tracks = tracks[0]
    
    return tracks, reason


async def spotrec_tracks(spotify, count=1):
    """recommendation - fetch a number of spotify recommendations for a specific user
    
    takes:
        spotify: spotify connection object
        token: spotify user token for suggestion
        seeds: a list of tracks
    """
    procname = "spotrec_tracks"
    
    
    # use the recent positively rated tracks as seeds
    recent_tracks = await PlayHistory.filter().order_by('-id').limit(20).prefetch_related('rating', 'track')
        
    seed_tracks = [x.track for x in recent_tracks if x.rating and x.rating.rating > 0]
    seed_tracks = seed_tracks[:5]
    seed_spotifyids = [x.spotifyid for x in seed_tracks]
    seed_names = [x.trackname for x in seed_tracks]

    logging.debug("%s getting spotify recommendations", procname)
    try:
        utrack = await spotify.recommendations(track_ids=seed_spotifyids, limit=count)
    except tk.TooManyRequests as e:
        retry_after = e.response.headers['retry-after']
        logging.error("%s rate limit timeout - sleeping for %s", procname, retry_after)
        await asyncio.sleep(retry_after)
        return [], "too many requests"
    
    except Exception as e:
        logging.error("%s exception getting recommendations: %s", procname, e)
        return [], "error getting recommendations"
    
    tracks = [await trackinfo(spotify, spotifyid=x.id) for x in utrack.tracks]
    reason = "spotify recommendations based on %s" % ", ".join(seed_names)
        
    if len(tracks) == 0:
        logging.warning("%s no potential tracks to queue", procname)
        tracks = []
    
    if len(tracks) == 1:
        tracks = tracks[0]
    
    return tracks, reason


async def get_fresh_tracks(count=1):
    """get a list of tracks that haven't been rated by the current listeners"""
    users = await getactiveusers()
    user_ids = [user.id for user in users]
    user_displaynames = [user.displayname for user in users]
    subquery = Rating.filter(user_id__in=user_ids).values('track_id')
    tracks = await ( Track.annotate(order=Random())
                          .exclude(id__in=Subquery(subquery))
                          .exclude(duration_ms__lte=60000)
                          .exclude(duration_ms__gte=420000)
                          .order_by('order')
                          .limit(count))
    reason = "fresh tracks for %s" % ", ".join(user_displaynames)

    if len(tracks) == 1:
        return tracks[0], reason
    return tracks, reason


async def get_request(spotify, cred):
    """get a track from the requests playlist from a random active user"""
    #get a random active user
    active_users = await getactiveusers()
    request_candidates = {}
    
    # get the users that have a "requests" playlist
    for user in active_users:
        user, token = await getuser(cred, user)
        with spotify.token_as(token):
            try:
                playlists = await spotify.playlists(user.spotifyid)
            except tk.Unauthorised as e:
                logging.error("get_request unauthorized error for user %s: %s", user.displayname, e)
                continue
            except Exception as e:
                logging.error("get_request exception attempting to get playlists for user %s: %s", user.displayname, e)
                continue
            
            request_playlist_id = next((x.id for x in playlists.items if x.name == "requests"), None)
            if request_playlist_id:
            # get the tracks from the playlist
                try:
                    request_playlist = await spotify.playlist(request_playlist_id)
                except Exception as e:
                    logging.error("get_request exception attempting to get playlist %s for user %s: %s", request_playlist_id, user.displayname, e)
                    continue
                tracks = request_playlist.tracks.items
                
                if len(tracks) > 0:
                    # get one song at random from the playlist
                    # request is a tekore.model.PlaylistTrack object not a models.Track object
                    request = choice(tracks)
                    
                    # this call is done the user's token, we're still under token_as(token)
                    track = await trackinfo(spotify, spotifyid=request.track.id)
                    request_candidates = {user: (token, track)}
                    logging.debug("get_request found request from user %s: %s", user.displayname, track.trackname)
                    
    if not request_candidates:
        logging.debug("get_request no request candidates")
        return None, "no request candidates"
    
    # pick one request at random
    user = choice(list(request_candidates.keys()))
    token, track = request_candidates[user]
    logging.debug("get_request selected recommendation from user %s: %s", user.displayname, track.trackname)
    
    with spotify.token_as(token):
        # remove the track from the user's requests playlist
        try:
            await spotify.playlist_remove(request_playlist.id, [track.trackuri])
            logging.debug("get_request remove request from playlist for %s: %s", user.displayname, track.trackname)
        except Exception as e:
            logging.error("get_request exception attempting to remove track from playlist\nplaylistid=%s trackuri=%s: \n%s",
                          request_playlist.id, track.trackuri, e)
        
    if not track:
        logging.error("get_request - something weird went wrong at the end, no track")
        return None, "error selecting candidate"
    
    logging.debug("get_request recommendation, user %s: %s", user.displayname, track.trackname)
    reason = "%s's request" % user.displayname
    return track, reason
