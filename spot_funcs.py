"""spotify support functions"""

import asyncio
import logging
from tortoise.exceptions import IntegrityError
from pprint import pformat
from models import Track, PlayHistory, SpotifyID, WebTrack, Rating, Lock
from helpers import feelabout


DURATION_VARIANCE_MS = 60000  # 60 seconds in milliseconds

async def is_saved(spotify, token, track):
    """check whether a track has been saved to your Spotify saved songs"""
    with spotify.token_as(token):
        saved = await spotify.saved_tracks_contains([track.spotifyid])
    return saved[0]


async def trackinfo(spotify_object, check_spotifyid):
    """Pull track name (and details)

    Args:
        spotify (obj): Spotify object
        spotifyid (str): Spotify's unique track id

    Returns:
        track object
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
    spotify_details = await spotify_object.track(check_spotifyid)
    
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


async def was_recently_played(spotify, token, track):
    """check player history"""
    logging.debug("was_recently_played checking player history")
    with spotify.token_as(token):
        h = await spotify.playback_recently_played()
        tracknames = [" & ".join([artist.name for artist in x.track.artists]) + " - " + x.track.name  for x in h.items]
        if track.trackname in tracknames:
            return True
    return False


async def get_player_queue(spotify):
    """fetch the items in the player queue for a given user"""
    procname = "get_player_queue"
    logging.debug("%s fetching player queue", procname)
    try: 
        return await spotify.playback_queue()
    except Exception as e:
        logging.error("%s exception %s", procname, e)


async def is_already_queued(spotify, token, track: str):
    """check if track is in player's queue/context"""
    logging.debug("is_already_queued checking player queue")
    with spotify.token_as(token):
        h = await get_player_queue(spotify)
    tids = [x.id for x in h.queue]
        
    if track in tids:
        return True
    
    return False


async def send_to_player(spotify, token, track: Track):
    """send a track to a player's queue"""
    with spotify.token_as(token):
        try:
            _ = await spotify.playback_queue_add(track.trackuri)
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


async def queue_safely(spotify, token, state):
    """make sure this is a good rec before we queue it"""
    procname = "queue_safely"
    
    # make sure we have the lock before we do anything
    # if we can't get a lock, another server or process is already watching this user
    try:
        if not await Lock.attempt_acquire_lock(state.user.id):
            logging.warning("%s couldn't get queue lock for user %s, won't queue rec",
                            procname, state.user.displayname)
            return False
    except Exception as e:
        logging.error("queue_safely - Exception checking lock for user %s: %s", state.user.id, e)
        return False
    
    if state.nextup is None:
        # don't send a none
        logging.warning("%s no Recommendations, nothing to queue", procname)
        return False
    
    # don't queue songs this user hates
    rating = await Rating.get_or_none(user_id=state.user.id, track_id=state.nextup.track.id)
    if rating and rating.rating < -1:
        logging.warning("%s user has a negative rating (%s), won't sent rec to player: %s", 
                        procname, rating, state.nextup.track.trackname)
        return False
    
    # don't queue the track we're currently playing, dingus
    if state.track.id == state.nextup.track.id:
        logging.warning("%s track is playing now, won't send again, removing rec: %s",
                        procname, state.nextup.track.trackname)
        # and remove it from the queue
        await state.nextup.delete()
        return False
    
    # don't send a track we already played 
    # this may cause a problem down the road
    if await was_recently_played(spotify, token, state.nextup.track.spotifyid):
        logging.warning("%s track was played recently, won't send again, removing - %s",
                        procname, state.nextup.track.trackname)
        # and remove it from the queue
        await state.nextup.track.delete()
        return False
    
    # don't resend something that's already in the player queue/context
    # figure out the context and ignore those tracks
    if await is_already_queued(spotify, token, state.nextup.track.spotifyid):
        logging.warning("%s track already queued, won't send again - %s",
                        procname, state.nextup.track.trackname)
        return False
    
    # okay fine, queue it
    await send_to_player(spotify, token, state.nextup.track)
    logging.info("%s sent to player queue for %s: [%s] %s",
                        procname, state.user.displayname, state.nextup.reason, state.n())
    return True


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
        
