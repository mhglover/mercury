"""spotify support functions"""

async def is_saved(spotify, token, trackid):
    """check whether a track has be saved to your Spotify saved songs"""
    with spotify.token_as(token):
        saved = await spotify.saved_tracks_contains([trackid])
    return saved[0]
