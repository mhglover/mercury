"""the queue"""
import logging
from datetime import timezone as tz, datetime as dt, timedelta
import asyncio
from humanize import naturaltime
from tortoise.expressions import Q
from models import Recommendation, Track, Option, WebTrack, Rating
from users import getactiveusers
from blocktypes import popular_tracks, spotrec_tracks, get_fresh_tracks, get_request
from spot_funcs import get_live_recs, trackinfo
from raters import feelabout

QUEUE_SIZE = 4
BLOCK = "fresh popular popular spotrec request"
ENDZONE_THRESHOLD_MS = 30000

async def queue_manager(spotify, cred, sleep=10):
    """manage the queue"""
    procname = "queue_manager"
    logging.info('%s starting', procname)

    block = []
    
    while True:
        logging.debug("%s checking queue state", procname)

        # remove the old and busted
        await expire_queue()

        # get active recs
        recommendations = await get_live_recs()

        # too much, baby, trim that back
        while len(recommendations) > QUEUE_SIZE:
            newest = recommendations.pop()
            logging.warning("%s queue is too large, removing rec_id:%s track_id:%s trackname:%s",
                         procname, newest.id, newest.track_id, newest.trackname)
            await newest.delete()

        # oh honey, fix you a plate
        while len(recommendations) < QUEUE_SIZE:
            logging.debug("%s queue is too small, adding a track", procname)
            recommendations = await get_live_recs()
            
            activeusers = await getactiveusers()
            # activeuids = [x.spotifyid for x in activeusers]
            if len(activeusers) > 0:

                # get the next block makeup from the database
                if len(block) == 0:
                    block_makeup, _ = await ( Option.get_or_create(
                                                    option_name="block_makeup", 
                                                    defaults = { 
                                                        "option_value": BLOCK}))
                    block = block_makeup.option_value.split()
                # get the next recommendation type from the current block
                playtype = block.pop(0)

            else:
                playtype = "spotrec"
            
            logging.debug("%s getting a [%s] recommendation", procname, playtype)
            reason = playtype
            # pick the next track to add to the queue
            if playtype == "request":
                track, reason = await get_request(spotify, cred)
                if not track:
                    logging.debug("%s no request track returned, switching to popular", procname)
                    playtype = "popular"
            
            if playtype == "fresh":
                track, reason = await get_fresh_tracks()
                
            if playtype == "spotrec":
                track, reason = await spotrec_tracks(spotify)
            
            if playtype == "popular":
                track, reason = await popular_tracks()

            if not track:
                logging.error("%s no track returned, sleeping until the next loop", procname)
                await asyncio.sleep(sleep)
                continue

            logging.info("%s adding [%s]: %s (%s)", procname, playtype, track.trackname, reason)
            
            if track.id in [x.track_id for x in recommendations]:
                logging.error("queue_manager - track already in Recommendations, skipping: %s %s", track.id, track.trackname)
                continue
            
            # stupid inefficient way to ensure tracks get repaired before adding to recs
            track = await trackinfo(spotify, trackid=track.id)
            track = await trackinfo(spotify, spotifyid=track.spotifyid)
            
            u = await Recommendation.create(track_id=track.id,
                                            trackname=track.trackname,
                                            reason=reason)
            await u.save()
            recommendations.append(u)

        logging.debug("%s sleeping for %s", procname, sleep)
        await asyncio.sleep(sleep)


async def gettrack(track_id):
    """get a track from the database"""
    return await Track.get(id=track_id)


async def getnext(get_all=False, webtrack=False, user=None):
    """get the next track's details from the queue and database
    
    get_all: bool - return all recs rather than the next one
    
    webtrack: bool - return as a WebTrack object rather than a Recommendation
    
    user: add a user's rating to the WebTrack (only applies when webtrack=True)
    
    returns: Recommendation (prefetched track) or WebTrack
    """
    logging.debug("pulling queue from db")
    recs = await get_live_recs()
    
    if not recs:
        logging.error("getnext - no live recommendations found")
        return None
    
    if get_all:
        return recs
    else:
        rec = recs[0]
    
    if webtrack:
        if user is not None:
            rating = await Rating.get_or_none(track_id=rec.track_id,
                                        user_id=user.id).values_list('rating', flat=True)
        else:
            rating = 0
            
        if rating is None:
            rating = 0

        track = WebTrack( trackname=rec.trackname,
                          track_id=rec.track.id,
                          color=feelabout(rating),
                          rating=rating,
                          reason=rec.reason,
                        )
        return track
    
    if rec is None:
        logging.error("getnext returning None - why isn't there a ready recommendation?")
        return None
    
    logging.debug("getnext returning %s", rec.trackname)
    return rec


async def expire_queue() -> None:
    """remove old tracks from the upcoming queue"""
    now = dt.now(tz.utc)
    logging.debug("expire_queue removing tracks with expires_at before %s", now)
    expired = await Recommendation.filter(expires_at__lte=now)
    for each in expired:
        await each.delete()
        logging.info("expire_queue removed expired recommendation: %s %s", each.trackname, each.expires_at)
    
    # if there are any active users, remove tracks that have been queued for over an hour
    activeusers = await getactiveusers()
    if len(activeusers) == 0:
        return
    
    logging.debug("expire_queue removing tracks with creation times over 1 hours ago")
    expired = await Recommendation.filter(queued_at__lte=now - timedelta(hours=1)) 
    for each in expired:
        await each.delete()
        logging.info("expire_queue removed old recommendation: %s %s", each.trackname, each.queued_at)
