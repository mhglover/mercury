"""the queue"""
import logging
import datetime
import asyncio
from models import Recommendation, Track, Rating, WebTrack
from users import getactiveusers
from blocktypes import popular_tracks, spotrec_tracks, get_fresh_tracks
from spot_funcs import validatetrack

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

BLOCK = ["fresh", "popular", "popular", "spotrec"]

async def queue_manager(spotify, sleep=10):
    """manage the queue"""
    procname = "queue_manager"
    logging.info('%s starting', procname)

    block = []
    
    while True:
        logging.debug("%s checking queue state", procname)

        # remove the old and busted
        await expire_queue()

        # get the new hotness
        recommendations = await Recommendation.all()

        # too much, baby, trim that back
        while len(recommendations) > 2:
            newest = recommendations.pop()
            logging.info("%s queue is too large, removing latest trackid %s",
                         procname, newest)
            await Recommendation.filter(id=newest.id).delete()

        # oh honey, fix you a plate
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
                block = list(BLOCK)
                playtype = block.pop(0)
            else:
                playtype = block.pop(0)
            
            # pick the next track to add to the queue
            if playtype == "fresh":
                track = await get_fresh_tracks()
                
            if playtype == "spotrec":
                track = await spotrec_tracks(spotify)
            
            elif playtype == "popular":
                track = await popular_tracks()
            
            else:
                logging.error("%s nothing to recommend, we shouldn't be here", procname)

            logging.info("%s adding [%s] recommendation: %s", procname, playtype, track.trackname)
            
            # validate the track before we add it to the recommendations
            if not await validatetrack(spotify, track):
                logging.error("invalid track, don't recommend: [%s] %s", track.id, track.trackname)
                # just sleep and loop again
                logging.error("sleeping until the next loop")
                continue
            
            u = await Recommendation.create(track_id=track.id,
                                            trackname=track.trackname,
                                            reason=playtype)
            await u.save()
            recommendations.append(u)

        logging.debug("%s sleeping for %s", procname, sleep)
        await asyncio.sleep(sleep)


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
