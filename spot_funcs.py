"""spotify support functions"""

import asyncio
import logging
from datetime import timezone as tz, datetime as dt, timedelta as td
from tortoise.expressions import Q
from tortoise.exceptions import IntegrityError, MultipleObjectsReturned
from tortoise.transactions import in_transaction
import tekore as tk
from pprint import pformat
from models import Track, PlayHistory, SpotifyID, WebTrack, Rating, Lock, Recommendation
from helpers import feelabout
from users import getplayer


DURATION_VARIANCE_MS = 60000  # 60 seconds in milliseconds

async def is_saved(spotify, token, track):
    """check whether a track has been saved to your Spotify saved songs"""
    try:
        with spotify.token_as(token):
            saved = await spotify.saved_tracks_contains([track.spotifyid])
    except tk.Unauthorised as e:
        logging.error("is_saved - 401 Unauthorised exception %s", e)
    except Exception as e:
        logging.error("is_saved exception %s", e)
        return False
    return saved[0]


async def trackinfo(spotify, trackid=None, spotifyid=None, token=None):
    """Pull track name (and details)

    Args:
        spotify (obj): Spotify object
        spotifyid (str): Spotify's unique track id
        token (obj): Spotify token object

    Returns:
        track object or None
    """
    
    if trackid:
        logging.debug("trackinfo - fetching track by id: %s", trackid)
        try:
            track = await Track.get(id=trackid)
        except Exception as e:
            logging.error("trackinfo - exception querying Track table %s\n%s", trackid, e)
            track = None
        
        # fix tracks with no spotifyid field
        if track.spotifyid is None or track.spotifyid == "":
            logging.warning("trackinfo - track %s has no spotifyid, attempting to repair", trackid)
            if track.trackuri is not None:
                track.spotifyid = track.trackuri.split(":")[-1]
                try:
                    await track.save()
                    logging.warning("trackinfo - repaired spotifyid: %s", track.spotifyid)
                except Exception as e:
                    logging.error("trackinfo - exception saving track %s\n%s", trackid, e)
                    track = None
            else:
                logging.error("trackinfo - track %s has no spotifyid or trackuri, can't repair", trackid)
        
        # if we have a track, check for multiple SpotifyID entries
        spids = await SpotifyID.filter(track_id=trackid)
        
        if not spids:
            logging.error("trackinfo - no SpotifyID entries for track %s", trackid)
            if track.spotifyid is not None:
                logging.info("trackinfo - track has a spotifyid, attempting to create new spotifyid record: %s", track.spotifyid)
                try:
                    spid = await SpotifyID.create(spotifyid=track.spotifyid, track=track)
                except Exception as e:
                    logging.error("trackinfo - exception creating SpotifyID %s\n%s", track.spotifyid, e)
                    spid = None
                if spid:
                    logging.debug("trackinfo - created SpotifyID entry: %s", spid.id)
                    spids.append(spid)
                else:
                    logging.error("trackinfo - failed to create SpotifyID entry: %s", track.spotifyid)
            
        
        if len(spids) > 1:
            logging.debug("trackinfo - multiple SpotifyID entries for track %s", trackid)
            spotify_tracks = []
            
            for spid in spids:
                logging.debug("trackinfo - %s %s %s", spid.id, spid.spotifyid, spid.track_id)
                
                # pull the spotify track details for this spotifyid
                if token:
                    with spotify.token_as(token):
                        spotify_details = await spotify.track(spid.spotifyid, market="US")
                else:
                    spotify_details = await spotify.track(spid.spotifyid, market="US")
                
                # log any restrictions for troubleshooting
                if spotify_details.restrictions:
                    logging.warning("trackinfo - restrictions: %s", spotify_details)
                
                # if the retrieved spotifyid is the same as the retrieved one, it's canonical for the track
                if spotify_details.id == spid.spotifyid:
                    
                    trackartist = " & ".join([artist.name for artist in spotify_details.artists])
                    trackname = f"{trackartist} - {spotify_details.name}"
                    
                    # make sure the track record is correct
                    if track.spotifyid != spotify_details.id:
                        logging.warning("trackinfo - correcting non-canonical track spotifyid: (%s) %s", track.id, track.trackname)
                        track.spotifyid = spotify_details.id
                    
                    if track.trackuri != spotify_details.uri:
                        logging.warning("trackinfo - correcting trackuri: (%s) %s", track.id, track.trackname)
                        track.trackuri = spotify_details.uri
                    
                    if track.trackname != trackname:
                        logging.warning("trackinfo - correcting trackname: (%s) %s to %s", track.id, track.trackname, trackname)
                        track.trackname = trackname

                    try:
                        await track.save()
                    except IntegrityError as e:
                        logging.error("trackinfo - IntegrityError saving track %s\n%s", trackid, e)
                        logging.error("trackinfo - track: %s", track)
                    except Exception as e:
                        logging.error("trackinfo - exception saving track %s\n%s", trackid, e)
                        
                else:
                    logging.debug("trackinfo - non-canonical spotifyid: %s", spid.id)

                spotify_tracks.append(spid.spotifyid)
        else:
            logging.debug("trackinfo - only one SpotifyID entry for track %s", trackid)
            
        # clean up the main track record if necessary
        if token:
            with spotify.token_as(token):
                try:
                    spotify_details = await spotify.track(spids[0].spotifyid, market="US")
                except tk.TooManyRequests as e:
                    logging.error("trackinfo - 429 Too Many Requests exception, sleeping for five minutes\n%s", e)
                    asyncio.sleep(300)
                    return None
                
                except Exception as e:
                    logging.error("trackinfo - exception fetching spotify track: %s\n%s", type(e).__name__, e)
                    logging.info("spids: %s", spids)
                    return None
        else:
            try:
                spotify_details = await spotify.track(spids[0].spotifyid, market="US")
            except tk.TooManyRequests as e:
                    logging.error("trackinfo - 429 Too Many Requests exception, sleeping for five minutes\n%s", e)
                    asyncio.sleep(300)
                    return None
            except Exception as e:
                logging.error("trackinfo - exception fetching spotify track: %s\n%s", type(e).__name__, e)
                logging.info("spids: %s", spids)
                return None
            
        
        if track.spotifyid != spotify_details.id:
            logging.warning("trackinfo - correcting non-canonical track spotifyid: (%s) %s %s to %s", 
                            track.id, track.trackname, track.spotifyid, spotify_details.id)
            track.spotifyid = spotify_details.id
        
        if track.trackuri != spotify_details.uri:
            logging.warning("trackinfo - correcting trackuri: (%s) %s %s to %s", 
                            track.id, track.trackname, track.trackuri, spotify_details.uri)
            track.trackuri = spotify_details.uri
        
        trackartist = " & ".join([artist.name for artist in spotify_details.artists])
        trackname = f"{trackartist} - {spotify_details.name}"
        if track.trackname != trackname:
            logging.warning("trackinfo - correcting trackname: (%s) %s to %s", track.id, track.trackname, trackname)
            track.trackname = trackname
        
        try:
            await track.save()
        except IntegrityError as e:
            logging.error("trackinfo - IntegrityError saving track %s\n%s", trackid, e)
            logging.error("trackinfo - track: %s", track)
            dupe = await Track.filter(trackname=track.trackname).first()
            await dupe.delete()
            
        except Exception as e:
            logging.error("trackinfo - exception saving track %s\n%s", trackid, e)
            

        logging.debug("trackinfo - %s %s is_playable: %s restrictions: %s ", track.trackname,
                        spotify_details.id,
                        spotify_details.is_playable,
                        spotify_details.restrictions)
        
        return track
    
    # Check for this spotifyid in the database
    try:
        spid = await SpotifyID.filter(spotifyid=spotifyid).prefetch_related("track")
    except Exception as e:
        logging.error("trackinfo - exception querying SpotifyID table %s\n%s", spotifyid, e.json())
        spid = None

    if spid:
        logging.debug("trackinfo - spotifyid [%s] found in db", spotifyid)
        if token:
            with spotify.token_as(token):
                try:
                    spotify_details = await spotify.track(spotifyid, market="US")
                except Exception as e:
                    logging.error("trackinfo - exception fetching spotify track: %s\n%s", type(e).__name__, e)
                    return None
        else:
            try:
                spotify_details = await spotify.track(spotifyid, market="US")
            except Exception as e:
                logging.error("trackinfo - exception fetching spotify track: %s\n%s", type(e).__name__, e)
                return None
        
        # if we have extras, consolidate them where possible
        if len(spid) > 1:
            logging.warning("trackinfo - multiple instances of SpotifyID found, using the first: %s", spotifyid)
            
            for x in spid:
                logging.info("trackinfo - %s %s %s", x.id, x.track.trackname, x.track.spotifyid)
                
                # for each row after the first, if the track is the same, delete the spid object
                if x is not spid[0]:
                    if spid[0].track_id == x.track_id:
                        logging.warning("trackinfo - duplicate SpotifyID found, deleting: %s", x.id)
                        await x.delete()
                        await x.save()
                    else:
                        logging.warning("trackinfo - duplicate SpotifyID found, not deleting\nspid: %s\ndupe: %s", spid[0], x)
            

        return spid[0].track
    
    # we don't have this version of this track in the db, fetch it from Spotify
    logging.info("trackinfo - spotifyid not in db %s", spotifyid)
    try:
        if token:
            with spotify.token_as(token):
                spotify_details = await spotify.track(spotifyid, market="US")
        else:
            logging.debug("trackinfo - no token provided, fetching track without token")
            spotify_details = await spotify.track(spotifyid, market="US")
    except tk.Unauthorised as e:
        logging.error("trackinfo - 401 Unauthorised exception %s", e)
        return None
    except Exception as e:
        logging.error("trackinfo - exception fetching spotify track: %s\n%s", type(e).__name__, e)
        return None
    
    # check the track's markets and reject it if it's not available in the US
    if spotify_details.is_playable is not True:
        logging.error("trackinfo - track not playable: %s", spotifyid)
        
        if 'US' not in spotify_details.available_markets:
            logging.error("trackinfo - track not available in US: %s", spotifyid)
        
        if spotify_details.available_markets == []:
            logging.error("trackinfo - track not available in any markets: %s", spotifyid)
        
        return None

    trackartist = " & ".join([artist.name for artist in spotify_details.artists])
    trackname = f"{trackartist} - {spotify_details.name}"
    
    logging.debug("trackinfo - fetched track [%s] - %s", spotifyid, trackname)
    
    # Check if we already have this track in the database
    similar_tracks = (await Track.filter(trackname=trackname).order_by('id'))
    
    # if there already is a similar track, just link the spotifyid to it
    if len(similar_tracks) > 0:
        track = similar_tracks[0]
        logging.info("trackinfo - found similar track, linking - %s", track.trackname)
        # create a SpotifyID entry for this track
        try:
            sid, created = await SpotifyID.get_or_create(spotifyid=spotifyid, track=track)
        except Exception as e:
            logging.error("trackinfo - exception creating SpotifyID %s\n%s", spotifyid, e)
            sid = None
        
        # consolidate any extra versions of the track
        if len(similar_tracks) > 1:
            logging.warning("trackinfo - multiple similar tracks found, consolidating")
            track = await consolidate_tracks(similar_tracks)
        
        return track

    # No preexisting version, so create the track
    logging.info("trackinfo - new track [%s] %s", spotify_details.id, trackname)
    try:
        track = await Track.create(
                            duration_ms=spotify_details.duration_ms,
                            trackuri=spotify_details.uri,
                            trackname=trackname,
                            spotifyid=spotify_details.id
                            )
    except Exception as e:
        logging.error("trackinfo - exception creating track %s\n%s", spotifyid, e.json())

    # and create the SpotifyID entry
    try:
        sid, created = await SpotifyID.get_or_create(spotifyid=spotifyid, track=track)
    except Exception as e:
        logging.error("trackinfo - exception creating SpotifyID %s\n%s", spotifyid, e)
        sid = None
    
    if created:
        logging.debug("trackinfo - linked spotifyid [%s][%s] to [%s][%s] %s",
                    sid.id, sid.spotifyid, track.id, track.spotifyid, track.trackname)   

    return track


async def get_webtrack(track, user=None):
    """accept a track object and return a webtrack"""
    
    if track is None:
        logging.error("get_webtrack - no track provided")
        return None
    
    track = await normalizetrack(track)
    
    if user is not None:
        rating = await (Rating.get_or_none(track_id=track.id, user_id=user.id))
    
    wt = WebTrack(trackname=track.trackname,
                  track_id=track.id,
                  template_id=f"track_{track.id}",
                  comment=rating.comment if rating else "",
                  color=feelabout(rating.rating if rating else 0),
                  rating=rating.rating if rating else 0)
    return wt


async def getrecents(limit=10):
    """pull recently played tracks from history table
    
    returns: list of track ids"""
    try:
        ph = await PlayHistory.all().order_by('-id').limit(limit).prefetch_related("track")
    except Exception as e:
        logging.error("getrecents - exception querying playhistory table %s", e)

    return ph


def truncate_middle(s, n=30):
    """shorten long names"""
    if len(s) <= n:
        # string is already short-enough
        return s
    if n <= 3:
        return s[:n] # Just return the first n characters if n is very small
     # half of the size, minus the 3 .'s
    n_2 = (n - 3) // 2
    # whatever's left
    n_1 = n - n_2 - 3
    return '{0}...{1}'.format(s[:n_1], s[-n_2:])


async def was_recently_played(state, rec=None):
    """check player history"""
    spotify = state.spotify
    token = state.token
    
    try:
        with spotify.token_as(token):
            h = await spotify.playback_recently_played()
            tracknames = [" & ".join([artist.name for artist in x.track.artists]) + " - " + x.track.name  for x in h.items]
    except tk.Unauthorised as e:
        logging.error("was_recently_played - 401 Unauthorised exception %s", e)
        logging.error("was_recently_played - token expiring: %s, expiration: %s", token.is_expiring, token.expires_in)
    except Exception as e:
        logging.error("was_recently_played - exception fetching player history %s", e)
        tracknames = None
        return False, tracknames
    
    if rec:
        checktrack = rec.track
    else:
        checktrack = state.track
    
    if checktrack.trackname in tracknames:
        logging.debug("was_recently_played track was recently played: %s", state.track.trackname)
        return True, tracknames
    
    logging.debug("was_recently_played track was not recently played: %s", state.track.trackname)
    return False, tracknames


async def get_player_queue(state):
    """fetch the items in the player queue for a given user"""
    procname = "get_player_queue"
    logging.debug("%s fetching player queue", procname)
    spotify = state.spotify
    try:
        with spotify.token_as(state.token):
            currently_queue = await spotify.playback_queue()
    except tk.Unauthorised as e:
        logging.error("%s 401 Unauthorised exception %s", procname, e)
        return None
    except Exception as e:
        logging.error("%s exception %s", procname, e)
        return None
    
    if not currently_queue:
        logging.warning("%s no queue items found", procname)
        return None

    logging.debug("%s currently playing: %s", procname, currently_queue.currently_playing.name)
    logging.debug("%s queue (%s items): %s", procname, len(currently_queue.queue), [x.name for x in currently_queue.queue])
    return currently_queue


async def is_already_queued(state, track):
    """check if track is in player's queue/context"""
    logging.debug("is_already_queued checking player queue")
    spotify = state.spotify
    with spotify.token_as(state.token):
        h = await get_player_queue(state)
        if h is None:
            return False
        tracknames = [" & ".join([artist.name for artist in x.artists]) + " - " + x.name  for x in h.queue]
        if track.trackname in tracknames:
            logging.debug("is_already_queued track is already queued: %s", track.trackname)
            return True
    return False


async def send_to_player(spotify, token, track: Track):
    """send a track to a player's queue"""
    with spotify.token_as(token):
        try:
            _ = await spotify.playback_queue_add(track.trackuri)
        
        except tk.Unauthorised as e:
            logging.error("send_to_player - 401 Unauthorised exception %s", e)
            logging.error("send_to_player - token expiring: %s, expiration: %s", token.is_expiring, token.expires_in)
            return False
        except tk.NotFound as e:
            logging.error("send_to_player - 404 Not Found exception - trackuri: %s", track.trackuri)
            return False
        except Exception as e:
            logging.error("send_to_player - exception from spotify.playback_queue_add %s\n%s", type(e).__name__, track.trackname)
            if "502: Bad gateway" in str(e):
                logging.warning(
                    "send_to_player - 502 Bad gateway error occurred, retrying once: %s",
                    track.trackname
                )
                await asyncio.sleep(1)  # Wait for 1 second before retrying
                try:
                    _ = await spotify.playback_queue_add(track.trackuri)
                except Exception as e:
                    logging.error(
                        "send_to_player - retry failed, unable to add track to queue: %s\n%s",
                        track.trackname, e
                    )
            else:
                logging.error(
                    "send_to_player - unknown exception from spotify.playback_queue_add %s\n%s",
                    track.trackname, type(e).__name__
                )
            return False
        return True


async def queue_safely(state):
    """ check whether the user needs a recommendation and queue the best one """
    procname = "queue_safely"
    
    spotify = state.spotify
    token = state.token
    good_recs = []
    queue_is_locked = await Lock.check_for_lock(state.user.id)
    
    logging.debug("%s --- %s check whether we can and should send a recommendation", procname, state.user.displayname)
    
    # check if the user has a rec in the first five items in the queue/context
    recs, rec_in_queue = await get_recs_in_queue(state)
    
    # if we can't get the recs, we can't send to the queue safely, so bail
    if recs is None:
        logging.error("%s --- %s checking queue failed, can't send to queue safely", procname, state.user.displayname)
        
        if queue_is_locked:
            # release the lock
            logging.debug("%s --- %s releasing queue lock", procname, state.user.displayname)
            await Lock.release_lock(state.user.id)

        return False
    
    # we have a rec in the queue, don't send another
    if await is_rec_queued(state):
        logging.debug("%s --- %s already has rec in queue/context, no rec needed", procname, state.user.displayname)
        
        if queue_is_locked:
            logging.debug("%s --- %s queue is locked, but there's a rec in queue, releasing queue lock", procname, state.user.displayname)
            await Lock.release_lock(state.user.id)
        
        return False
    
    # no rec in the queue but it's locked, don't send
    if queue_is_locked:
        logging.warning("%s --- %s no rec in queue, but queue is locked, can't send to queue safely", procname, state.user.displayname)
        return False
    
    # lock the queue so that nobody else can send a rec to this user while we do
    logging.debug("%s --- %s no rec in queue, locking queue", procname, state.user.displayname)
    lock = await Lock.attempt_acquire_lock(state.user.id)
    
    if not lock:
        logging.error("%s --- %s failed to acquire queue lock, can't send to queue safely", procname, state.user.displayname)
        return False
    
    logging.info("%s --- %s no rec found, locked queue, verifying currently playing: [%s] %s", procname, state.user.displayname, state.track.spotifyid, state.track.trackname)
    suspected = state.currently
    state.currently = await getplayer(state)
    if state.currently.item.id != suspected.item.id:
        state.track = await trackinfo(spotify, spotifyid=state.currently.item.id, token=state.token)
        logging.info("%s --- %s track changed while locking queue, now playing: %s", procname, state.user.displayname, state.track.trackname)

    # discard any recs that are not valid
    for rec in recs:
        # get this user's rating for this rec
        rating = await Rating.get_or_none(user_id=state.user.id, track_id=rec.track_id)
        
        # don't send a track with a recent playhistory for this user
        # this check should catch most of the cases to avoid
        interval = dt.now() - td(minutes=90)
        in_history = await PlayHistory.exists(track_id=rec.track_id, user_id=state.user.id, played_at__gte=interval)
        if in_history:
            logging.debug("%s --- %s rec has recent playhistory, not a good candidate: %s",
                        procname, state.user.displayname, rec.trackname)
            continue
        else:
            logging.debug("%s --- %s rec doesn't have recent playhistory: %s", procname, state.user.displayname, rec.trackname)
        
        logging.debug("%s --- %s considering rec: %s", procname, state.user.displayname, rec.trackname)
        
        # don't send a track that's currently playing (should have a recent PlayHistory)
        if state.track.id == rec.track_id:
            logging.debug("%s --- %s rec playing now, not a good candidate: %s",
                        procname, state.user.displayname, rec.trackname)
            
            if rec.expires_at is None:
                logging.debug("%s --- %s currently playing rec has no expiration: %s", procname, state.user.displayname, rec.trackname)
            
            continue
        else:
            logging.debug("%s --- %s rec not currently playing: %s", procname, state.user.displayname, rec.trackname)
        
        # if a rec was played in the last cycle, don't send that either (should have a recent PlayHistory)
        if rec.track_id == state.track_last_cycle.id:
            logging.debug("%s --- %s rec was played last cycle, not a good candidate: %s",
                        procname, state.user.displayname, rec.trackname)
            continue
        else:
            logging.debug("%s --- %s rec doesn't match track_last_cycle: (rec) %s vs. (prior)%s", procname, state.user.displayname, rec.trackname, state.track_last_cycle.trackname)
        
        # if we've played this rec recently, don't send it again (should have a recent PlayHistory)
        track_was_recently_played, recent_tracks = await was_recently_played(state, rec=rec)
        if track_was_recently_played:
            logging.debug("%s --- %s rec was recently played, won't try to replay: %s",
                        procname, state.user.displayname, rec.trackname)
            continue
        else:
            logging.debug("%s --- %s rec.trackname not in recent_tracks: %s", procname, state.user.displayname, rec.trackname)
        
        # don't send a disliked track (must be a track liked by other active listeners, but not this one)
        if rating:
            if rating.rating < 0:
                logging.warning("%s negative rating, won't sent rec to player: %s - (%s) %s",
                            procname, rating.rating, state.user.displayname, rec.trackname)
                continue
            else:   
                logging.debug("%s rec rating is acceptable: %s - (%s) %s ", procname, rating.rating, state.user.displayname, rec.trackname)
        
        
        # don't send a track that's not available in the user's market
        # with spotify.token_as(token):
        #    check_track = await spotify.track(rec.track.spotifyid)
    
        # if 'US' not in check_track.available_markets:
        #     logging.error("%s --- %s track not available in US: %s (%s)", 
        #                   procname, state.user.displayname, 
        #                   rec.trackname, rec.track.spotifyid)
        #     logging.error("%s --- %s available markets: %s",
        #                   procname, state.user.displayname, 
        #                   check_track.available_markets)
        #     logging.error("%s --- %s check track: %s",
        #                   procname, state.user.displayname,
        #                   check_track)
            
            # clean up the track if we can
            # logging.warning("%s --- %s cleaning up track: %s",
            #                 procname, state.user.displayname, rec.trackname)
            # await trackinfo(spotify, spotifyid=rec.track.spotifyid, token=token)
            # cleaned_track = await trackinfo(spotify, trackid=rec.track_id, token=token)
            # logging.warning("%s --- %s cleaned track: %s",
            #                 procname, state.user.displayname, cleaned_track)

            # continue
        
        # made it through the gauntlet of tests, this is an acceptable rec
        logging.debug("%s --- %s adding rec to candidates: %s", procname, state.user.displayname, rec.trackname)
        good_recs.append(rec)
        
    if len(good_recs) < 1:
        logging.warning("%s --- %s no valid Recommendations, nothing to queue", procname, state.user.displayname)
        return False
    
    # okay fine, queue the first rec
    first_rec = good_recs[0]
    # get the track object for this rec with the canonical spotifyid
    queueing = await trackinfo(spotify, trackid=first_rec.track_id, token=token)
    
    logging.info("%s --- %s sending first rec to queue: (%s) [%s] %s",
                        procname, state.user.displayname, queueing.id, queueing.spotifyid, queueing.trackname)
    sent_successfully = await send_to_player(spotify, token, queueing)
    
    if not sent_successfully:
        logging.error("%s --- %s failed to send rec to queue: %s (%s)", 
                      procname, state.user.displayname, first_rec.trackname, first_rec.reason)
        return False
    
    # # wait a tick for the  queue touch to take effect
    await asyncio.sleep(1)
    
    # make sure the rec was put in the queue
    recs, rec_in_queue = await get_recs_in_queue(state)
    
    if await is_rec_queued(state):
        logging.info("%s --- %s sent rec and confirmed a rec is in queue: [%s] [%s] %s (%s)", procname, state.user.displayname, first_rec.track_id, first_rec.track.spotifyid, first_rec.trackname, first_rec.reason)
        # release the lock
        logging.debug("%s --- %s releasing queue lock", procname, state.user.displayname)
        await asyncio.sleep(3)
        await Lock.release_lock(state.user.id)
        return True
    else:
        logging.error("%s --- %s sent rec (%s) but track not in queue: %s (%s)\n%s", procname, state.user.displayname, first_rec.track.spotifyid, first_rec.trackname, first_rec.reason, first_rec)
    
        recs = await Recommendation.all().prefetch_related("track")
        for rec in recs:
            logging.error("rec: %s %s", rec.track.spotifyid, rec.trackname)
    
        queue_context = await get_player_queue(state)
        logging.error("currently: %s %s", queue_context.currently_playing.id, queue_context.currently_playing.name)
    
        for track in queue_context.queue:
            logging.error("q/c: %s %s", track.id, track.name)
        
        # release the lock
        logging.debug("%s --- %s releasing queue lock", procname, state.user.displayname)
        await Lock.release_lock(state.user.id)
        return False


async def normalizetrack(track):
    """figure out where a track is and return it"""
    if not track:
        logging.error("normalizetrack no track provided")
        return None
    
    if isinstance(track, int):
        logging.debug("normalizetrack fetching Track record by id: %s", track)
        track = await Track.get(id=track)
    
    if isinstance(track, str):
        id_type = "id" if len(track) < 7 else "spotifyid"
        logging.debug("normalizetrack fetching Track record by %s: %s", id_type, track)
        track = await Track.get(**{id_type: track})
    
    if isinstance(track, Track):
        logging.debug("normalizetrack this is a Track object: [%s] %s", track.id, track.trackname)
        if track.id is None and track.spotifyid is not None:
            track = await Track.get(spotifyid=track.spotifyid)

    else:
        logging.error("normalizetrack this isn't a string or a track object: %s", type(track))
        logging.error(pformat(track))

    logging.debug("normalizetrack normalized track: [%s] %s", track.id, track)
    
    return track


async def consolidate_tracks(tracks):
    """if we have multiple tracks that are the same, consolidate them"""
    
    if len(tracks) == 1:
        tracks = await Track.filter(trackname=tracks[0].trackname)
    
    if len(tracks) < 2:
        logging.info("consolidate_tracks only one track, nothing to consolidate")
        return tracks[0]
    
    # we have multiple tracks that are the same, consolidate them
    logging.info("consolidate_tracks consolidating %s tracks", len(tracks))
    
    # check each track to see if the spotifyid is the same
    
    # get the original track, based on the lowest id record
    original_track = tracks[0]
    
    for t in tracks[1:]:
        logging.info("consolidate_tracks %s into %s", t.id, original_track.id)
        logging.info(t)
        
        # update the spotifyids associated with this track to point to the original
        t_spotifyids = await SpotifyID.filter(track=t)
        
        for sid in t_spotifyids:
            logging.debug("consolidate_tracks updating SpotifyID %s to track %s", 
                         sid.id, original_track.id)
            sid.track_id = original_track.id
            await sid.save()
        
        ratings = await Rating.filter(track_id=t.id)
        
        for rating in ratings:
            rating.track_id = original_track.id
            try:
                await rating.save()
            except IntegrityError as e:
                logging.debug("consolidate_tracks exception updating Rating %s\n%s", rating.id, e)
                logging.debug("consolidate_tracks deleting duplicate Rating %s", rating.id)
                await rating.delete()
                
            except Exception as e:
                logging.error("consolidate_tracks exception updating Rating %s\n%s", rating.id, e)
        
        playistories = await PlayHistory.filter(track_id=t.id)
        for playhistory in playistories:
            logging.debug("consolidate_tracks updating PlayHistory %s to track %s", 
                         playhistory.id, original_track.id)
            playhistory.track_id = original_track.id
            try:
                await playhistory.save()
            except Exception as e:
                logging.error("consolidate_tracks exception updating PlayHistory %s\n%s",
                              playhistory.id, e)

        # delete the track
        try:
            await t.delete()
        except Exception as e:
            logging.error("consolidate_tracks exception deleting track %s\n%s", t.id, e)
            
        return original_track


async def get_recs_in_queue(state, rec=None):
    """ get the current recommendations and check if any are in the player queue 
    
    returns: list of recs, bool
    """
    spotify = state.spotify
    token = state.token
    # only include recs with no expiration
    recs = await get_live_recs()
    
    # get the list of tracks waiting in the player queue, plus the tracks that
    # are forthcoming in the player's context, ie album, playlist, artist, etc
    
    try:
        with spotify.token_as(token):
            queue_context = await get_player_queue(state)
            
    except tk.Unauthorised as e:
        logging.error("user_has_rec_in_queue 401 Unauthorised exception %s", e)
        logging.error("token expiring: %s, expiration: %s", state.token.is_expiring,state.token.expires_in)
        return None, False
    
    except Exception as e:
        logging.error("user_has_rec_in_queue exception fetching player queue %s", e)
        return None, False
    
    if queue_context is None:
        logging.warning("user_has_rec_in_queue no queue items, that's weird: %s", state.user.displayname)
        return None, False
    
    rec_names = [x.trackname for x in recs]
    
    # only check the first five items in the queue, we don't care about stuff deep in the context
    queue_names = [" & ".join([artist.name for artist in x.artists]) + " - " + x.name for x in queue_context.queue[:]]

    # if the top queue_name is a rec return the recs list, True
    # check the top five queue_names for matches with rec_names
    if any(queue_name in rec_names for queue_name in queue_names):
        logging.debug("get_recs_in_queue found rec in queue: %s", queue_names[0])
        return recs, True
    
    return recs, False


async def get_live_recs():
    """get a list of recommendations that haven't expired yet with prefetched tracks"""
    now = dt.now(tz.utc)
    recs = ( await Recommendation.filter(Q(expires_at__isnull=True) | 
                                       Q(expires_at__gt=now))
                               .order_by('id')
                               .prefetch_related("track"))
    return recs


async def is_rec_queued(state) -> bool:
    """check if a recommendation is already queued"""

    spotify = state.spotify
    token = state.token
    # get all the spotifyids for the recs that haven't expired yet
    # gotta fix the model so this can be done with tortoise
    sql = "SELECT s.spotifyid FROM public.recommendation r left join spotifyid s on r.track_id=s.track_id;"
    
    try:
        async with in_transaction() as connection:
            _, results = await connection.execute_query(sql)
    except Exception as e:
        logging.error("is_rec_queued - exception fetching recs %s", e)
        return False

    logging.debug("is_rec_queued - fetched rec spotifyids")
    
    # get the list of tracks waiting in the player queue, plus the tracks that
    # are forthcoming in the player's context, ie album, playlist, artist, etc
    try:
        if token:
            with spotify.token_as(token):
                queue_context = await get_player_queue(state)
        else:
            queue_context = await get_player_queue(state)
            
    except tk.Unauthorised as e:
        logging.error("is_rec_queued - user_has_rec_in_queue 401 Unauthorised exception %s", e)
        logging.error("is_rec_queued - token expiring: %s, expiration: %s", state.token.is_expiring,state.token.expires_in)
        return False
    
    except Exception as e:
        logging.error("is_rec_queued - user_has_rec_in_queue exception fetching player queue %s", e)
        return False
    
    if queue_context is None:
        logging.warning("is_rec_queued - user_has_rec_in_queue no queue items, that's weird: %s", state.user.displayname)
        return False
    
    logging.debug("is_rec_queued - pulled queue/context from spotify")
    
    # does any rec_spotid appear in queue_spotids?
    rec_spotids = [x['spotifyid'] for x in results]
    queue_spotids = [x.id for x in queue_context.queue]
    queued_recs = [x in queue_spotids for x in rec_spotids]
    
    if any(queued_recs):
        logging.debug("is_rec_queued found rec in queue: %s", rec_spotids[0])
        return True

    # no rec found in queue
    for x in rec_spotids:
        logging.debug("rec_spotifyid: %s", x)
    
    recs = await Recommendation.all().prefetch_related("track")
    for rec in recs:
        logging.debug("rec: %s %s", rec.track.spotifyid, rec.trackname)
    
    queue_context = await get_player_queue(state)
    logging.debug("currently: %s %s", queue_context.currently_playing.id, queue_context.currently_playing.name)
    
    for track in queue_context.queue:
        logging.debug("q/c: %s %s", track.id, track.name)
    
    return False
