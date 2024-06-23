"""the queue"""
import logging
import datetime
import asyncio
import pickle
from models import Recommendation, Track, PlayHistory, Rating
from users import getactiveusers
from blocktypes import popular_tracks, spotrec_tracks

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace


async def queue_manager(spotify):
    """manage the queue"""
    procname = "queue_manager"
    logging.info('%s starting', procname)

    block = []
    
    while True:
        logging.debug("%s checking queue state", procname)

        recommendations = await Recommendation.all()
        # try:
        #     uqueue = [x.trackid for x in iter(query)]
        # except Exception as e:
        #     logging.error("%s failed pulling queue from database, exception type: %e\n%s",
                        #   procname, type(e), e)

        await expire_queue()

        while len(recommendations) > 2:
            newest = recommendations.pop()
            logging.info("%s queue is too large, removing latest trackid %s",
                         procname, newest)
            await Recommendation.filter(id=newest.id).delete()

        while len(recommendations) < 2:
            logging.debug("%s queue is too small, adding a track", procname)
            activeusers = await getactiveusers()
            # activeuids = [x.spotifyid for x in activeusers]
            if len(activeusers) == 0:
                logging.info("%s no active listeners, sleeping for 60 seconds", procname)
                await asyncio.sleep(60)
                continue
            
            logging.debug("BLOCK STATE: %s", block)
            if len(block) == 0:
                block = ["pop", "pop", "spotrec"]
                playtype = block.pop(0)
            else:
                playtype = block.pop(0)
            
            # pick the next track to add to the queue
            if playtype == "spotrec":
                first = activeusers[0]
                token = pickle.loads(first.token)
                
                logging.info("%s queuing a spotify recommendation", procname)
                seeds = await popular_tracks(5)
                upcoming_tid = await spotrec_tracks(spotify, token, seeds)

            elif playtype == "pop":
                logging.info("%s queuing a popular recommendation", procname)
                upcoming_tid = await popular_tracks()
            
            else:
                logging.error("%s nothing to recommend, we shouldn't be here", procname)

            track = await gettrack(upcoming_tid)
            logging.info("%s adding to radio queue: %s", procname, track.trackname)
            
            u = await Recommendation.create(track_id=upcoming_tid,
                                            trackname=track.trackname)
            await u.save()
            recommendations.append(upcoming_tid)

        logging.debug("%s sleeping for %s", procname, 10)
        await asyncio.sleep(10)


async def gettrack(tid):
    """get a track from the database"""
    return await Track.get(id=tid)


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


async def getrecents(spotify):
    """pull recently played tracks from history table
    
    returns: list of track ids"""
    try:
        ph_query = await PlayHistory.all().order_by('-id').limit(10)
    except Exception as e:
        logging.error("exception ph_query %s", e)

    try:
        tracks = [await trackinfo(spotify, x.trackid) for x in ph_query]
        playhistory = [x.trackname for x in tracks]
    except Exception as e:
        logging.error("exception playhistory %s", e)

    return playhistory


async def getratings(trackids, uid):
    """pull ratings for a list of tracks for a given user"""
    ratings = []
    for tid in trackids:
        r = await Rating.filter(userid=uid).filter(trackid=tid).get()
        if r.rating >= 1:
            color = "love"
        elif r.rating == 1:
            color = "like"
        elif r.rating == 0:
            color = "shrug"
        elif r.rating == -1:
            color = "dislike"
        elif r.rating <= -2:
            color = "hate"
                
    ratings.append((r.trackname, color))
    return ratings
    

async def getnext():
    """get the next track's details from the queue and database
    
    returns: recommendation object with track prefetched
    """
    logging.debug("pulling queue from db")
    return await Recommendation.first().prefetch_related("track")


async def expire_queue():
    """remove old tracks from the upcoming queue"""
    now = datetime.datetime.now(datetime.timezone.utc)
    logging.debug("expire_queue removing old tracks")
    expired = await Recommendation.filter(expires_at__lte=now)
    for each in expired:
        logging.info("expire_queue removing track: %s %s",
                     each.trackname, each.expires_at)
        _ = await Recommendation.filter(id=each.id).delete()
