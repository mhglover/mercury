"""spotify support functions"""

import logging
from models import Track, PlayHistory, SpotifyID

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace
# pylint: disable=trailing-newlines


async def is_saved(spotify, token, trackid):
    """check whether a track has been saved to your Spotify saved songs"""
    with spotify.token_as(token):
        saved = await spotify.saved_tracks_contains([trackid])
    return saved[0]


async def trackinfo(spotify, spotifyid):
    """Pull track name (and details)

    Args:
        spotify (obj): Spotify object
        spotifyid (str): Spotify's unique track id

    Returns:
        track object
    """
    # Check if the Spotify ID already exists
    spotify_id_entry = await SpotifyID.filter(spotifyid=spotifyid).first()

    if spotify_id_entry:
        # Fetch the associated track
        logging.debug("trackinfo - spotifyid [%s] found in db, fetching track", spotifyid)
        track = await Track.get(id=spotify_id_entry.track_id)
    else:
        logging.info("trackinfo - spotifyid not in db %s", spotifyid)
        
        # what is this?
        spotify_details = await spotify.track(spotifyid)
        
        # do we have an alternative version already in the db?
        if spotify_details.linked_from is None:
        
            # Create the track
            logging.info("trackinfo - creating track for [%s]", spotifyid)
            trackartist = " & ".join([artist.name for artist in spotify_details.artists])
            trackname = f"{trackartist} - {spotify_details.name}"
            track, _ = await Track.get_or_create(
                                        duration_ms=spotify_details.duration_ms,
                                        trackuri=spotify_details.uri,
                                        trackname=trackname,
                                        defaults={
                                            'spotifyid': spotifyid
                                        })
            
            # Create the SpotifyID entry
            logging.info("trackinfo - creating spotifyid for %s", trackname)
            await SpotifyID.create(spotifyid=spotifyid, track=track)
            
        else:
            logging.warning("trackinfo - spotifyid [%s] linked to [%s], recursively fetching track",
                            spotifyid, spotify_details.linked_from.id)
            track = trackinfo(spotify, spotify_details.linked_from.id)

    return track


async def validatetrack(spotify, track):
    """for a track in the database, validate it's playable"""
    logging.debug("validatetrack validating a track: %s", track)
    
    if isinstance(track, str):
        logging.debug("validatetrack this is a string, fetching Track record: %s", track)
        track = await Track.get(spotifyid=track)
    elif isinstance(track, Track):
        logging.debug("validatetrack this is a Track object: [%s] %s", track.id, track.trackname)
    elif isinstance(track, int):
        logging.debug("validatetrack this is an int, fetching Track record: %s", track)
        track = await Track.get(id=track)
    else:
        logging.error("this isn't a string or a track object: %s", type(track))
        logging.error(track)

    # make sure we have a canonical spotifyid in the Track record
    if not track.spotifyid:
        logging.warning("validatetrack track missing canonical spotifyid: [%s] %s", track.id, track.trackname)

    # check the spotid table for this track
    spotifyids = await track.spotifyids.all()
    
    if len(spotifyids) > 1:
        logging.warning("validatetrack found %s spotify ids for this track", len(spotifyids))
    
    # cheack each of these
    for spotifyid in spotifyids:
        
        # does this exist in Spotify?
        spot_track = await spotify.track(spotifyid.spotifyid, market='US')
        
        if track.spotifyid == spotifyid.spotifyid:
            logging.info("validatetrack canonical spotifyid: [%s]", spotifyid.spotifyid)
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
