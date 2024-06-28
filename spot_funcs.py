"""spotify support functions"""

import logging
from models import Track, PlayHistory, SpotifyID

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace
# pylint: disable=trailing-newlines


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
    # Check if the Spotify ID already exists
    spotify_id_entry = await SpotifyID.filter(spotifyid=check_spotifyid).first()

    if spotify_id_entry:
        # Fetch the associated track
        logging.debug("trackinfo - spotifyid [%s] found in db, fetching track", check_spotifyid)
        track = await Track.get(id=spotify_id_entry.track_id)
    else:
        logging.debug("trackinfo - spotifyid not in db %s", check_spotifyid)
        
        # what is this?
        spotify_details = await spotify_object.track(check_spotifyid)
        
        # do we have an alternative version already in the db?
        if spotify_details.linked_from is None:
        
            # Create or fetch the track
            logging.debug("trackinfo - new track [%s]", spotify_details.id)
            trackartist = " & ".join([artist.name for artist in spotify_details.artists])
            trackname = f"{trackartist} - {spotify_details.name}"
            track, created = await Track.get_or_create(
                                        duration_ms=spotify_details.duration_ms,
                                        trackuri=spotify_details.uri,
                                        trackname=trackname,
                                        defaults={
                                            'spotifyid': spotify_details.id
                                        })
            
            if created:
                logging.debug("trackinfo created track [%s][%s] for %s",
                             track.id, track.spotifyid, track.trackname)
            
            # Create the SpotifyID entry
            sid, created = await SpotifyID.get_or_create(spotifyid=check_spotifyid, track=track)
            
            if sid.spotifyid != track.spotifyid:
                if created:
                    logging.info("trackinfo - created and linked spotifyid [%s][%s] to [%s][%s] %s",
                         sid.id, sid.spotifyid, track.id, track.spotifyid, track.trackname)
                else:
                    logging.info("found SpotifyId [%s][%s] linked to Track [%s][%s] %s",
                         sid.id, sid.spotifyid, track.id, track.spotifyid, track.trackname)
            
        else:
            logging.warning("trackinfo - spotifyid [%s] linked to [%s], recursively fetching track",
                            sid.spotifyid, spotify_details.linked_from.id)
            track = trackinfo(spotify_object, spotify_details.linked_from.id)

    return track


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
    
    if len(spotifyids) > 1:
        logging.warning("validatetrack found %s spotify ids for this track", len(spotifyids))
    
    # cheack each of these
    for spotifyid in spotifyids:
        
        # does this exist in Spotify?
        spot_track = await spotify.track(spotifyid.spotifyid, market='US')
        
        if track.spotifyid == spotifyid.spotifyid:
            logging.debug("validatetrack canonical spotifyid: [%s]", spotifyid.spotifyid)
        else:
            logging.info("validatetrack secondary spotifyid: [%s]", spotifyid.spotifyid)
        
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
            logging.error("validatetrack track is unplayable [%s], rejecting", spotifyid.spotifyid)
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
    return '{0}...{1}'.format(s[:n_1], s[-n_2:]) # pylint: disable=consider-using-f-string


async def was_recently_played(spotify, token, track: str):
    """check player history"""
    logging.debug("was_recently_played checking player history")
    with spotify.token_as(token):
        h = await spotify.playback_recently_played()
        tids = [x.track.id for x in h.items]
        if track in tids:
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
            logging.error(
                "%s exception spotify.playback_queue_add %s\n%s",
                "send_to_player", track.trackname, e)


async def queue_safely(spotify, token, state):
    """make sure this is a good rec before we queue it"""
    procname = "queue_safely"
    
    if state.nextup is None:
        # don't send a none
        logging.warning("%s no Recommendations, nothing to queue", procname)
        return False
    
    # don't queue the track we're currently playing, dingus
    if state.track.id == state.nextup.track.id:
        logging.warning("%s track is playing now, won't send again, removing rec: %s",
                        procname, state.nextup.track.trackname)
        # and remove it from the queue
        await state.nextup.track.delete()
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
    logging.info("%s sent to player queue [%s] %s",
                        procname, state.nextup.reason, state.n())
    return True
    


def copy_track_data(original_track):
    """Creates a copy of the track data without saving a new row in the database"""
    if original_track.id is None:
        return Track()
    
    new_track = Track(
        spotifyid=original_track.spotifyid,
        trackname=original_track.trackname,
        trackuri=original_track.trackuri,
        duration_ms=original_track.duration_ms
    )
    return new_track


async def normalizetrack(track):
    """figure out where a track is and return it"""
    
    if isinstance(track, str):
        # this is a stupid way to test this 
        if len(track) < 7:
            logging.debug("normalizetrack this is a short string, fetching Track record: %s", track)
            track = await Track.get(id=track)
        else:
            logging.debug("normalizetrack this is a short string, fetching Track record: %s", track)
            track = await Track.get(spotifyid=track)
    
    elif isinstance(track, Track):
        logging.debug("normalizetrack this is a Track object: [%s] %s", track.id, track.trackname)
        if track.id is None and track.spotifyid is not None:
            s = str(track.spotifyid)            
            track = await Track.get(spotifyid=s)
    else:
        logging.error("normalizetrack this isn't a string or a track object: %s", type(track))
        logging.error(track)
    
    return track

