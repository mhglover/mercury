"""spotify support functions"""

import asyncio
import logging
import datetime as dt
from tortoise.expressions import Q
from tortoise.exceptions import IntegrityError
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


async def trackinfo(spotify_object, check_spotifyid, token=None):
    """Pull track name (and details)

    Args:
        spotify (obj): Spotify object
        spotifyid (str): Spotify's unique track id

    Returns:
        track object or None
    """
    
    # Check if the Spotify ID has already been added to the database
    spotify_id_entry = await SpotifyID.filter(spotifyid=check_spotifyid).first()

    if spotify_id_entry:
        # Fetch the associated track
        logging.debug("trackinfo - spotifyid [%s] found in db, fetching track", check_spotifyid)
        track = await Track.get(id=spotify_id_entry.track_id)
        
        # Check if we have a similar track in the database, within a certain duration
        min_duration = track.duration_ms - DURATION_VARIANCE_MS
        max_duration = track.duration_ms + DURATION_VARIANCE_MS
        similar_tracks = (await Track
                            .filter(duration_ms__gte=min_duration, duration_ms__lte=max_duration)
                            .filter(trackname=track.trackname)
                            .order_by('id')
                            .all())
    
        if len(similar_tracks) > 1:
            track = similar_tracks[0]
            logging.info("trackinfo - found %s similar tracks, consolidating - %s",
                         len(similar_tracks),
                         track.trackname)
            await consolidate_tracks(similar_tracks)
        
        return track
    
    
    logging.debug("trackinfo - spotifyid not in db %s", check_spotifyid)
    
    # we don't have this version of this track in the db, fetch details from Spotify
    try:
        if token:
            with spotify_object.token_as(token):
                spotify_details = await spotify_object.track(check_spotifyid)
        else:
            spotify_details = await spotify_object.track(check_spotifyid)
        
        
    except tk.Unauthorised as e:
        logging.error("trackinfo - 401 Unauthorised exception %s", e)
        return None
    
    trackartist = " & ".join([artist.name for artist in spotify_details.artists])
    trackname = f"{trackartist} - {spotify_details.name}"
    
    # does this spotify track actually point to another track? recurse
    if spotify_details.linked_from is not None:
        logging.warning("trackinfo - spotifyid [%s] is linked to [%s], recursively fetching track",
                        check_spotifyid, spotify_details.linked_from.id)
        track = trackinfo(spotify_object, spotify_details.linked_from.id)
        return track
    
    # Check if we have a similar track in the database, within a certain duration
    min_duration = spotify_details.duration_ms - DURATION_VARIANCE_MS
    max_duration = spotify_details.duration_ms + DURATION_VARIANCE_MS
    similar_tracks = (await Track
                        .filter(duration_ms__gte=min_duration, duration_ms__lte=max_duration)
                        .filter(trackname=trackname)
                        .order_by('id')
                        .all())
    
    if len(similar_tracks) > 1:
        track = similar_tracks[0]
        logging.info("trackinfo - found %s similar tracks, consolidating - %s",
                     len(similar_tracks), track.trackname)
        await consolidate_tracks(similar_tracks)
    else:
        # Create the track
        logging.debug("trackinfo - new track [%s] %s", spotify_details.id, trackname)
        track = await Track.create(
                                duration_ms=spotify_details.duration_ms,
                                trackuri=spotify_details.uri,
                                trackname=trackname,
                                spotifyid=spotify_details.id
                                )

    # Create the SpotifyID entry
    try:
        sid, created = await SpotifyID.get_or_create(spotifyid=check_spotifyid, track=track)
    except Exception as e:
        logging.error("trackinfo - exception creating SpotifyID %s\n%s", check_spotifyid, e)
        sid = None
    
    if created:
        logging.debug("trackinfo - linked spotifyid [%s][%s] to [%s][%s] %s",
                    sid.id, sid.spotifyid, track.id, track.spotifyid, track.trackname)   

    return track


async def get_webtrack(track, user=None):
    """accept a track object and return a webtrack"""
    
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


async def validatetrack(spotify, track):
    """for a track in the database, validate it's playable"""
    logging.debug("validatetrack validating a track: %s", track)
    
    track = await normalizetrack(track)
    
    # make sure we have a canonical spotifyid in the Track record
    if not track.spotifyid:
        logging.warning("validatetrack track missing canonical spotifyid: [%s] %s",
                        track.id, track.trackname)

    # check the spotid table for this track
    spotifyids = await track.spotifyids.all()
    logging.debug("validatetrack found %s spotify ids for this track", len(spotifyids))
    
    # check each of these
    for spotifyid in spotifyids:
        
        # does this exist in Spotify?
        try:
            spot_track = await spotify.track(spotifyid.spotifyid, market='US')
        except Exception as e:
            logging.error("validatetrack exception fetching spotify track %s", e)
            spot_track = None
        
        if track.spotifyid == spotifyid.spotifyid:
            logging.debug("validatetrack canonical spotifyid: [%s]", spotifyid.spotifyid)
        else:
            logging.debug("validatetrack secondary spotifyid: [%s]", spotifyid.spotifyid)
        
        # does this exist in spotify?
        if not spot_track:
            logging.error("validatetrack no spotify track for id [%s], removing SpotifyId record",
                          spotifyid.spotifyid)
            _ = await SpotifyID.get(spotifyid=spotifyid.spotifyid).delete()
            
            if track.spotifyid == spotifyid.spotifyid:
                logging.error("validatetrack removing canonical spotifyid from Track record")
                track.spotifyid = ""
                track.save()

        # okay, it's real.  is this playable in the US?
        if not spot_track.is_playable:
            logging.error("validatetrack rejected unplayable track: %s [%s](%s)",
                          track.trackname, spotifyid.spotifyid, spot_track.restrictions)
            return False
    
    return True


async def getrecents(limit=10):
    """pull recently played tracks from history table
    
    returns: list of track ids"""
    try:
        ph = await PlayHistory.all().order_by('-id').limit(limit).prefetch_related("track")
    except Exception as e:
        logging.error("exception querying playhistory table %s", e)

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
        logging.error("was_recently_played 401 Unauthorised exception %s", e)
        logging.error("token expiring: %s, expiration: %s", token.is_expiring, token.expires_in)
    except Exception as e:
        logging.error("was_recently_played exception fetching player history %s", e)
        tracknames = None
    
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
            return await spotify.playback_queue()
    except tk.Unauthorised as e:
        logging.error("%s 401 Unauthorised exception %s", procname, e)
    except Exception as e:
        logging.error("%s exception %s", procname, e)
    return None


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
            logging.error("token expiring: %s, expiration: %s", token.is_expiring, token.expires_in)
        except Exception as e:
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
                    track.trackname, e
                )


async def queue_safely(state):
    """ check whether the user needs a recommendation and queue the best one """
    procname = "queue_safely"
    
    spotify = state.spotify
    token = state.token
    good_recs = []
    
    logging.debug("%s --- %s needs a recommendation", procname, state.user.displayname)
    
    # do we need to send a recommendation to the player queue?
    # no if the recommendation is in the queue within top 5 positions
    
    recs, rec_in_queue = await get_recs_in_queue(state)
    if recs is None:
        logging.error("%s --- %s checking queue failed, can't send to queue safely", procname, state.user.displayname)
        return False
    
    if rec_in_queue:
        logging.debug("%s --- %s already has rec in queue/context, no rec needed", procname, state.user.displayname)
        return False
    
    # check for a lock on the queue
    if await Lock.check_for_lock(state.user.id):
        logging.warning("%s --- %s queue locked, can't send to queue safely", procname, state.user.displayname)
        return False
    
    # lock the queue so that we don't allow multiple servers to send recs at the same time
    await Lock.attempt_acquire_lock(state.user.id)
    
    logging.info("%s --- %s no rec found, verifying currently playing: %s", procname, state.user.displayname, state.track.trackname)
    state.currently = await getplayer(state)
    state.track = await trackinfo(spotify, state.currently.item.id, token=state.token)
    logging.info("%s --- %s updated currently playing: %s", procname, state.user.displayname, state.track.trackname)

    # discard any recs that are not valid
    for rec in recs:
        # get this user's rating for this rec
        rating = await Rating.get_or_none(user_id=state.user.id, track_id=rec.track_id)
        
        # don't send a track with a recent playhistory for this user
        # this check should catch most of the cases to avoid
        interval = dt.datetime.now() - dt.timedelta(minutes=90)
        in_history = await PlayHistory.exists(track_id=rec.track_id, user_id=state.user.id, played_at__gte=interval)
        if in_history:
            logging.warning("%s --- %s rec has recent playhistory, not a good candidate: %s",
                        procname, state.user.displayname, rec.trackname)
            continue
        else:
            logging.debug("%s --- %s rec doesn't have recent playhistory: %s", procname, state.user.displayname, rec.trackname)
        
        logging.debug("%s --- %s considering rec: %s", procname, state.user.displayname, rec.trackname)
        
        # don't send a track that's currently playing (should have a recent PlayHistory)
        if state.track.id == rec.track_id:
            logging.warning("%s --- %s rec playing now, not a good candidate: %s",
                        procname, state.user.displayname, rec.trackname)
            
            if rec.expires_at is None:
                logging.warning("%s --- %s currently playing rec has no expiration: %s", procname, state.user.displayname, rec.trackname)
            
            continue
        else:
            logging.debug("%s --- %s rec not currently playing: %s", procname, state.user.displayname, rec.trackname)
        
        # if a rec was played in the last cycle, don't send that either (should have a recent PlayHistory)
        if rec.track_id == state.track_last_cycle.id:
            logging.warning("%s --- %s rec was played last cycle, not a good candidate: %s",
                        procname, state.user.displayname, rec.trackname)
            continue
        else:
            logging.debug("%s --- %s rec doesn't match track_last_cycle: (rec) %s vs. (prior)%s", procname, state.user.displayname, rec.trackname, state.track_last_cycle.trackname)
        
        # if we've played this rec recently, don't send it again (should have a recent PlayHistory)
        track_was_recently_played, recent_tracks = await was_recently_played(state, rec=rec)
        if track_was_recently_played:
            logging.warning("%s --- %s rec was recently played, won't try to replay: %s",
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
        
        # made it through the gauntlet of tests, this is an acceptable rec
        logging.debug("%s --- %s adding rec to candidates: %s", procname, state.user.displayname, rec.trackname)
        good_recs.append(rec)
        
    if len(good_recs) < 1:
        logging.warning("%s --- %s no valid Recommendations, nothing to queue", procname, state.user.displayname)
        return False
    
    # okay fine, queue the first rec
    first_rec = good_recs[0]
    await send_to_player(spotify, token, first_rec.track)
    logging.debug("%s --- %s sent first rec to queue: %s (%s)",
                        procname, state.user.displayname, first_rec.trackname, first_rec.reason)
    
    # wait a tick for the queue touch to take effect
    await asyncio.sleep(1)
    
    # make sure the rec was put in the queue
    recs, rec_in_queue = await get_recs_in_queue(state)
    
    # unlock the queue
    await Lock.release_lock(state.user.id)
    
    if first_rec in recs:
        logging.info("%s --- %s sent rec and confirmed track in queue: %s (%s)", procname, state.user.displayname, first_rec.trackname, first_rec.reason)
        return True
    else:
        logging.error("%s --- %s sent rec but track not in queue: %s (%s)", procname, state.user.displayname, first_rec.trackname, first_rec.reason)
        return False


async def normalizetrack(track):
    """figure out where a track is and return it"""
    
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
    
    if len(tracks) < 2:
        return False
    
    # we have multiple tracks that are the same, consolidate them
    logging.debug("consolidate_tracks consolidating %s tracks", len(tracks))
    
    # get the original track, based on the lowest id record
    original_track = tracks[0]
    
    for t in tracks[1:]:
        logging.debug("consolidate_tracks consolidating %s into %s", t.id, original_track.id)
        
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
            
        return True


async def get_recs_in_queue(state, rec=None):
    """ check if the queue or context has any of the current recommendations"""
    spotify = state.spotify
    token = state.token
    # only include recs with no expiration
    recs = ( await Recommendation.filter(Q(expires_at=None) | 
                                         Q(expires_at__gte=dt.datetime.now(dt.timezone.utc)))
                                 .order_by('id')
                                 .prefetch_related("track"))
    
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