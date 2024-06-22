"""the queue"""
import logging
import datetime
import asyncio
import pickle
from models import UpcomingQueue, Track, PlayHistory
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

        query = await UpcomingQueue.all()
        try:
            uqueue = [x.trackid for x in iter(query)]
        except Exception as e:
            logging.error("%s failed pulling queue from database, exception type: %e\n%s",
                          procname, type(e), e)

        await expire_queue()

        while len(uqueue) > 2:
            newest = uqueue.pop()
            logging.info("%s queue is too large, removing latest trackid %s",
                         procname, newest)
            await UpcomingQueue.filter(trackid=newest).delete()

        while len(uqueue) < 2:
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

            trackname = await trackinfo(spotify, upcoming_tid)
            logging.info("%s adding to radio queue: %s", procname, trackname)
            
            u = await UpcomingQueue.create(trackid=upcoming_tid)
            await u.save()
            uqueue.append(upcoming_tid)

        logging.debug("%s sleeping for %s", procname, 10)
        await asyncio.sleep(10)


async def trackinfo(spotify, trackid, return_track=False, return_time=False):
    """pull track name (and details))

    Args:
        trackid (str): Spotify's unique track id
        return_track (bool, optional): also return the track. Defaults to False.
        return_time (bool, optional): also return the track duration

    Returns:
        str: track artist and title
        str, track object: track artist and title, track object
    """
    track, created = await Track.get_or_create(trackid=trackid,
                                      defaults={
                                          "duration_ms": 0,
                                          "trackname": "",
                                          "trackuri": ""
                                          })
    
    if created or track.trackuri == '' or track.duration_ms == '':
        spotify_details = await spotify.track(trackid)
        trackartist = " & ".join([x.name for x in spotify_details.artists])
        track.trackname = f"{trackartist} - {spotify_details.name}"
        track.duration_ms = spotify_details.duration_ms
        track.trackuri = spotify_details.uri
        await track.save()

    name = track.trackname

    if return_time:
        milliseconds = track.duration_ms
        seconds = int(milliseconds / 1000) % 60
        minutes = int(milliseconds / (1000*60)) % 60
        name = f"{track.trackname} {minutes}:{seconds:02}"

    if return_track is True:
        return name, track
    
    return name


async def getrecents(spotify):
    """pull recently played tracks from history table"""
    try:
        ph_query = await PlayHistory.all().order_by('-id').limit(10)
    except Exception as e:
        logging.error("exception ph_query %s", e)

    try:
        playhistory = [await trackinfo(spotify, x.trackid) for x in ph_query]
    except Exception as e:
        logging.error("exception playhistory %s", e)

    return playhistory


async def getnext():
    """get the next track's details from the queue and database
    
    returns: trackid, expires_at
    """
    logging.debug("pulling queue from db")
    n = await UpcomingQueue.filter().order_by("id").limit(1)
    
    if n == []:
        return None, None
    
    return n[0].trackid, n[0].expires_at


async def expire_queue():
    """remove old tracks from the upcoming queue"""
    now = datetime.datetime.now(datetime.timezone.utc)
    logging.debug("expire_queue removing old tracks")
    expired = await UpcomingQueue.filter(expires_at__lte=now)
    for each in expired:
        logging.info("expire_queue removing track: %s %s",
                     each.trackid, each.expires_at)
        _ = await UpcomingQueue.filter(id=each.id).delete()
