"""the queue"""
import logging
from datetime import timezone as tz, datetime as dt, timedelta
import asyncio
from humanize import naturaltime
from tortoise.expressions import Q
from models import Recommendation, Track, Option, WebTrack, Rating
from users import getactiveusers
from blocktypes import popular_tracks, spotrec_tracks, get_fresh_tracks, get_request
from spot_funcs import validatetrack
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

        # get the new hotness
        recommendations = await Recommendation.all()

        # too much, baby, trim that back
        while len(recommendations) > QUEUE_SIZE:
            newest = recommendations.pop()
            logging.info("%s queue is too large, removing latest trackid %s",
                         procname, newest)
            await Recommendation.filter(id=newest.id).delete()

        # oh honey, fix you a plate
        while len(recommendations) < QUEUE_SIZE:
            logging.debug("%s queue is too small, adding a track", procname)
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
            
            
            logging.info("%s getting a [%s] recommendation", procname, playtype)
            # pick the next track to add to the queue
            if playtype == "fresh":
                track = await get_fresh_tracks()
                
            elif playtype == "spotrec":
                track = await spotrec_tracks(spotify)
            
            elif playtype == "popular":
                track = await popular_tracks()
            
            elif playtype == "request":
                track = await get_request(spotify, cred)
            
            else:
                logging.error("%s invalid playtype [%s] returned", procname, playtype)
                # dropping out of the while loop, try again later
                break

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


async def getnext(get_all=False, webtrack=False, user=None):
    """get the next track's details from the queue and database
    
    get_all: bool - return all recs rather than the next one
    
    webtrack: bool - return as a WebTrack object rather than a Recommendation
    
    user: add a user's rating to the WebTrack (only applies when webtrack=True)
    
    returns: Recommendation (prefetched track) or WebTrack
    """
    logging.debug("pulling queue from db")
    if get_all:
        # this includes recommendations where expires_at is null or in the future
        rec = await (Recommendation.filter(Q(expires_at__isnull=True) | 
                                           Q(expires_at__gt=dt.now(tz.utc)))
                                   .order_by("id")
                                   .prefetch_related("track")
                    )
        return rec
    
    rec =  await (Recommendation.first()
                                .filter(Q(expires_at__isnull=True) | 
                                        Q(expires_at__gt=dt.now(tz.utc)))
                                .order_by("id")
                                .prefetch_related("track"))
    
    if rec and webtrack:
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
                          rating=rating
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
    logging.debug("expire_queue removing old tracks")
    expired = await Recommendation.filter(expires_at__lte=now)
    for each in expired:
        logging.info("expire_queue removing track: %s %s", each.trackname, each.expires_at)
        await each.delete()


async def set_rec_expiration(recommendation, remaining_ms) -> None:
    """set the timestamp for expiring a recommendation"""
    now = dt.now(tz.utc)
    expiration_interval = timedelta(milliseconds=(remaining_ms - ENDZONE_THRESHOLD_MS))
    recommendation.expires_at = now + expiration_interval
    logging.debug("set_rec_expiration - %s %s", recommendation.trackname, recommendation.expires_at)
    await recommendation.save()
    return recommendation.expires_at
