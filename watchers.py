"""reaper task"""
import os
import logging
import asyncio
import datetime
import pickle
from models import User, Recommendation, Track
from users import getuser
from queue_manager import getnext
from raters import rate, record
from spot_funcs import is_saved, trackinfo, truncate_middle

# pylint: disable=trailing-whitespace
# pylint: disable=broad-exception-caught

async def user_reaper():
    """check the database every 5 minutes and remove inactive users"""
    procname="user_reaper"
    sleep = 300
    idle = 20
    while True:
        logging.debug("%s checking for inactive users every %s seconds", procname, sleep)
        interval = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=idle)
        # actives = await User.filter(last_active__gte=interval).exclude(status="inactive")
        expired  = await User.filter(last_active__lte=interval).exclude(status="inactive")
        # for user in actives:
        #     interval = datetime.datetime.now(datetime.timezone.utc) - user.last_active
        #     logging.info("%s active user %s last active %s ago",
        #                 procname, user.spotifyid, interval)
        for user in expired:
            logging.info("%s marking user as inactive after %s minutes idle: %s",
                        procname, idle, user.spotifyid)
            user.status = "inactive"
            await user.save(update_fields=['status'])
        await asyncio.sleep(sleep)


async def watchman(taskset, cred, spotify, watcher, user):
    """start a watcher for active users"""
    procname=f"watchman_{user.displayname}"
    watchname=f"watcher_{user.displayname}"
    
    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager')
    if "spotify_watcher" not in run_tasks:
        logging.warning("%s this instance doesn't run spot watchers", procname)
        return
    
    if watchname in [x.get_name() for x in asyncio.all_tasks()]:
        logging.debug("%s is already running, won't start another", watchname)
        return

    logging.debug("%s creating a spotify watcher for: %s", 
                    procname, watchname)
    user_task = asyncio.create_task(watcher(cred, spotify, user),
                    name=watchname)
    
    # add this user task to the global tasks set
    added = taskset.add(user_task)
    if added is not None:
        logging.error("%s failed adding task to taskset?")


    # else:
    #     while True:
    #         procname="night_watchman"
    #         logging.info("%s checking for active users without watchers every %s seconds",
    #                       procname, sleep)
    #         actives = User.filter(status="active")
    #         for user in actives:
    #             interval = datetime.datetime.now(datetime.timezone.utc) - user.last_active
    #             logging.info("%s active user %s last active %s ago",
    #                         procname, user.spotifyid, interval)
            
    #         await asyncio.sleep(sleep)
    


async def get_player_queue(spotify, token, userid):
    """fetch the items in the player queue for a given user"""
    procname = "get_player_queue"
    logging.debug("%s fetching player queue for %s", procname, userid)
    with spotify.token_as(token):
        try: 
            playbackqueue = await spotify.playback_queue()
        except Exception as e:
            logging.error("%s exception %s", procname, e)
    return playbackqueue


async def spotify_watcher(cred, spotify, user):
    """start a long-running task to monitor a user's spotify usage, rate and record plays"""
    
    logging.debug("fetching user for watcher: %s", user)
    user, token = await getuser(cred, user)
    
    procname = f"watcher_{user.displayname}"
    logging.info("%s watcher starting", procname)

    # do some timestamp math and formatting
    now = datetime.datetime.now(datetime.timezone.utc)
    soon = datetime.timedelta(minutes=20)
    timestamp = now.strftime('%s')
    ttl = now + soon
    # then = datetime.timedelta(minutes=1)
    # recent = now - then
    
    # set this as the current watcher in the database
    user.watcherid = f"watcher_{user.spotifyid}_{timestamp}"
    await user.save()

    # Check the initial status
    with spotify.token_as(token):
        try:
            currently = await spotify.playback_currently_playing()
        except Exception as e:
            logging.error("%s spotify_currently_playing exception %s", procname, e)
        
        if currently is None:
            logging.debug("%s not currently playing", procname)
            track = Track()
            sleep = 30
            # track = None
        elif currently.is_playing is False:
            logging.debug("%s paused", procname)
            sleep = 30
            # track = None
        else:
            
            trackid = currently.item.id
            track = await trackinfo(spotify, trackid)
            trackname = track.trackname
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            position = currently.progress_ms/currently.item.duration_ms
            
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60
            logging.info("%s initial status - playing %s, %s:%0.02d remaining",
                         procname, trackname, minutes, seconds)

    # Loop while alive
    logging.debug("%s starting loop", procname)

    while ttl > datetime.datetime.now(datetime.timezone.utc):
        
        logging.debug("%s loop is awake", procname)
        user = await User.get(spotifyid=user.spotifyid)
        status = user.status
        
        # if user.watcherid == "killswitch":
        #     logging.warning("%s detected killswitch, unsetting killswitch and exiting", procname)
        #     user.watcherid = ""
        #     await user.save()
        #     return "killswitch"
        
        # if (user.watcherid != user.watcherid 
        #   and user.status != "inactive"
        #   and user.last_active > recent):
        #     logging.error("%s found another recent active watcher, exiting", procname)
        #     return "another active watcher"
 
        # refresh the spotify token if necessary
        if token.is_expiring:
            try:
                token = cred.refresh(token)
            except Exception as e:
                logging.error("getuser exception refreshing token\n%s", e)
            user.token = pickle.dumps(token)
            await user.save()

        logging.debug("%s checking currently playing", procname)
        with spotify.token_as(token):
            try:
                currently = await spotify.playback_currently_playing()
            except Exception as e:
                logging.error("%s exception in spotify.playback_currently_playing\n%s",procname, e)

        logging.debug("%s checking player queue state", procname)
        playbackqueue = await get_player_queue(spotify, token, user.spotifyid)
        playbackqueueids = [x.id for x in playbackqueue.queue]

        sleep = 30
        # not playing
        if currently is None:
            status = "not playing"
            trackid = None
            logging.debug("%s not currently playing", procname)

        # not playing - not paused?
        elif currently.currently_playing_type == "unknown":
            status = "not playing"
            trackid = None
            logging.debug("%s not currently playing", procname)

        # paused
        elif currently.is_playing is False:
            status = "paused"
            trackid = currently.item.id
            logging.debug("%s is paused", procname)

        # oh yeah now we cook
        else:
            
            # do we have anybody following us?
            followers = await User.filter(status=f"following:{user.displayname}")

            # update the status and ttl, keep the watcher alive for 20 minutes
            now = datetime.datetime.now(datetime.timezone.utc)
            soon = datetime.timedelta(minutes=20)
            ttl = now + soon
            logging.debug("%s updating ttl, last_active and status: %s", procname, ttl)
            user.last_active = now
            user.status = "active"
            await user.save()
            
            # update the ttl on followers too
            for each in followers:
                each.last_active = datetime.datetime.now(datetime.timezone.utc)
                await each.save()
            
            # note details from the last loop for comparison
            last_track = track
            last_position = position

            # pull details from the current item
            trackid = currently.item.id
            track = await trackinfo(spotify, trackid)
            trackname = track.trackname
            position = currently.progress_ms/currently.item.duration_ms
            
            # pull details for the next track in the queue
            nextup = await getnext()
            
            # do some math
            remaining_ms = currently.item.duration_ms - currently.progress_ms
            seconds = int(remaining_ms / 1000) % 60
            minutes = int(remaining_ms / (1000*60)) % 60

            # we aren't in the endzone yet
            if remaining_ms > 30000:
                
                # if we're playing the lead track in the Upcoming Queue
                # and nobody else has set the expiration yet
                if nextup.track.spotifyid == trackid and nextup.expires_at is None:
                    logging.info("%s first to start track %s, setting expiration",
                                 procname, truncate_middle(trackname))
                    
                    # set it for our endzone
                    # which we can calculate pretty closely
                    expires_at = (datetime.datetime.now(datetime.timezone.utc) + 
                                    datetime.timedelta(milliseconds=remaining_ms - 30000))
                    
                    _ = ( await Recommendation.select_for_update()
                                                .filter(id=nextup.id)
                                                .update(expires_at=expires_at))
                    
                    # record it in the playhistory table
                    # a recommendation was started by a player and we saw it
                    logging.debug("%s recording play history %s",
                                procname, nextup.track.trackname)
                    await record(spotify, user.spotifyid, nextup.track.spotifyid)


                # detect track changes
                if trackid != last_track.spotifyid:
                    logging.debug("%s track change at %.0d%% - now playing %s",
                                procname, last_position, trackname)
                    
                    # did we skip
                    if last_track.spotifyid == nextup.track.spotifyid:
                        logging.warning("%s removing skipped track from radio queue: %s",
                                    procname, last_track.trackname)
                        try:
                            await Recommendation.get(id=nextup.track_id).delete()
                        except Exception as e:
                            logging.error("%s exception removing track from queue\n%s",
                                        procname, e)
                    
                    # rate skipped tracks based on last position
                    if last_position < 0.33:
                        value = -2
                        logging.info("%s early skip rating, %s %s %s",
                                    user.spotifyid, last_track.trackname, value, procname)
                        await rate(user, last_track, value)
                    elif last_position < 0.7:
                        value = -1
                        logging.info("%s late skip rating, %s %s %s",
                                    user.spotifyid, last_track.spotifyid, value, procname)
                        await rate(spotify, user.spotifyid, last_track.trackname, value)
            
                if (remaining_ms - 30000) < 30000: # sleep for a few seconds
                    sleep = (remaining_ms - 30000) / 1000 # about to hit the autorate window
                else: # sleep for thirty seconds
                    sleep = 30

            # welcome to the end zone
            elif remaining_ms <= 30000:
                logging.info("%s endzone %s - next up %s",
                            procname, trackname, truncate_middle(nextup.trackname))
                
                # we got to the end of the track, so autorate
                # base on whether or not this is a saved track
                value = 4 if await is_saved(spotify, token, trackid) else 1
                logging.debug("%s setting a rating, %s %s %s", 
                             user.displayname, trackname, value, procname)
                await rate(user, track, value=value)
                
                # remove the track from dbqueue
                if trackid == nextup.track.spotifyid:
                    logging.info("%s first to endzone, removing track from radio queue: %s",
                                procname, truncate_middle(nextup.trackname))
                    try:
                        await Recommendation.filter(id=nextup.id).delete()
                    except Exception as e:
                        logging.error("%s exception removing track from upcomingqueue\n%s",
                                        procname, e)
                    
                    # now get the real next queued track
                    nextup = await getnext()
                    
                    # if there's nothing yet, fine, jump to the next cycle now
                    if nextup is None:
                        logging.warning("%s nothing in queue to queue, starting next loop early",
                                        procname)
                        continue

                # don't requeue something already forthcoming
                # this has potential to pause recommendations if we recommend something that was
                # already in the context queue, so let's warn about it
                logging.debug("%s checking player queue state", procname)
                playbackqueue = await get_player_queue(spotify, token, user.spotifyid)
                playbackqueueids = [x.id for x in playbackqueue.queue]
                
                if nextup.track.spotifyid in playbackqueueids:
                    logging.warning("%s track already queued, won't send again - %s",
                                    procname, nextup.track.trackname)
                else:
                    logging.info("%s sending to spotify queue %s",
                                    procname, nextup.track.trackname)
                    
                    with spotify.token_as(token):
                        try:
                            _ = await spotify.playback_queue_add(nextup.track.trackuri)
                        except Exception as e: 
                            logging.error(
                                "%s exception spotify.playback_queue_add %s\n%s",
                                procname, nextup.track.trackname, e)

                # sleep until this track is done
                sleep = (remaining_ms /1000) + 2

            # shorten long track names
            trackname = truncate_middle(trackname)
            status = f"{trackname} {position:.0%} {minutes}:{seconds:0>2} remaining"
            logging.debug("%s sleeping %0.2ds - %s", procname, sleep, status)
        
        await asyncio.sleep(sleep)

    # ttl expired, clean up before exit
    logging.info("%s timed out, cleaning house", procname)
    user.watcherid = ""
    user.status = "inactive"
    await user.save()
    
    logging.info("%s exiting", procname)
    return procname
