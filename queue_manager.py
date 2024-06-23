"""the queue"""
import logging
import datetime
import asyncio
import pickle
from models import Recommendation, Track, Rating, WebTrack
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
                block = ["spotrec","popular", "popular"]
                playtype = block.pop(0)
            else:
                playtype = block.pop(0)
            
            # pick the next track to add to the queue
            if playtype == "spotrec":
                first = activeusers[0]
                token = pickle.loads(first.token)
                
                logging.info("%s queuing a spotify recommendation", procname)
                seeds = await popular_tracks(5)
                rec = await spotrec_tracks(spotify, token, seeds)

            elif playtype == "popular":
                logging.info("%s queuing a popular recommendation", procname)
                rec = await popular_tracks(count=1)
            
            else:
                logging.error("%s nothing to recommend, we shouldn't be here", procname)

            logging.info("%s adding to radio queue: %s", procname, rec.trackname)
            
            u = await Recommendation.create(track_id=rec.id,
                                            trackname=rec.trackname)
            await u.save()
            recommendations.append(u)

        logging.debug("%s sleeping for %s", procname, 10)
        await asyncio.sleep(10)


async def gettrack(track_id):
    """get a track from the database"""
    return await Track.get(id=track_id)


async def getratings(trackids: list, uid: int):
    """pull ratings for a list of tracks for a given user"""
    ratings: list = []
    for tid in trackids:
        r = await ( Rating.filter(user_id=uid)
                          .filter(track_id=tid)
                          .get_or_none()
                          .prefetch_related("track"))
        if r is None:
            continue
        
        if r.rating > 1:
            color = "love"
        elif r.rating == 1:
            color = "like"
        elif r.rating == 0:
            color = "shrug"
        elif r.rating == -1:
            color = "dislike"
        elif r.rating <= -2:
            color = "hate"
        else:
            color = "what"

        ratings.append(WebTrack(track_id=r.track.id,
                                trackname=r.trackname,
                                color=color,
                                rating=r.rating))
        
    return ratings
    

async def getnext(get_all=False):
    """get the next track's details from the queue and database
    
    returns: recommendation object with track prefetched
    """
    logging.debug("pulling queue from db")
    if get_all:
        return await Recommendation.all().order_by("id").prefetch_related("track")
    else:
        return await Recommendation.first().order_by("id").prefetch_related("track")
    


async def expire_queue():
    """remove old tracks from the upcoming queue"""
    now = datetime.datetime.now(datetime.timezone.utc)
    logging.debug("expire_queue removing old tracks")
    expired = await Recommendation.filter(expires_at__lte=now)
    for each in expired:
        logging.info("expire_queue removing track: %s %s",
                     each.trackname, each.expires_at)
        _ = await Recommendation.filter(id=each.id).delete()
