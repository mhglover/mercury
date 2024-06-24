"""spotify support functions"""

import logging
from models import Track, PlayHistory

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace
# pylint: disable=trailing-newlines


async def is_saved(spotify, token, trackid):
    """check whether a track has been saved to your Spotify saved songs"""
    with spotify.token_as(token):
        saved = await spotify.saved_tracks_contains([trackid])
    return saved[0]


async def trackinfo(spotify, spotifyid):
    """pull track name (and details))

    Args:
        spotify (obj): spotify object
        trackid (str): Spotify's unique track id

    Returns:
        track object
    """
    track, created = await Track.get_or_create(spotifyid=spotifyid,
                                      defaults={
                                          "duration_ms": 0,
                                          "trackname": "",
                                          "trackuri": ""
                                          })
    
    if created or track.trackuri == '' or track.duration_ms == '':
        spotify_details = await spotify.track(spotifyid)
        trackartist = " & ".join([x.name for x in spotify_details.artists])
        track.trackname = f"{trackartist} - {spotify_details.name}"
        track.duration_ms = spotify_details.duration_ms
        track.trackuri = spotify_details.uri
        await track.save()
    
    return track


async def getrecents(limit=10):
    """pull recently played tracks from history table
    
    returns: list of track ids"""
    try:
        ph = await PlayHistory.all().order_by('-id').limit(limit).prefetch_related("track")
    except Exception as e:
        logging.error("exception querying playhistory table %s", e)

    return ph


def truncate_middle(s, n=15):
    """shorten long names"""
    if len(s) <= n:
        # string is already short-enough
        return s
    # half of the size, minus the 3 .'s
    n_2 = int(n) // 2 - 3
    # whatever's left
    n_1 = n - n_2 - 3
    return '{0}...{1}'.format(s[:n_1], s[-n_2:]) # pylint: disable=consider-using-f-string
