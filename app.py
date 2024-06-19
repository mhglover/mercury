#!/usr/bin/env python
"""mercury radio"""
import datetime
import logging
import os
import asyncio
import pickle
import tekore as tk
from dotenv import load_dotenv
from quart import Quart, request, redirect, render_template, session
from tortoise.contrib.quart import register_tortoise
from models import User, Track, UpcomingQueue
from watchers import user_reaper, watchman, spotify_watcher
from users import getactiveusers, getuser
from queue_manager import queue_manager, trackinfo, getrecents, getnext
from raters import rate_list, get_current_rating

# pylint: disable=W0718,global-statement
# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

load_dotenv()  # take environment variables from .env

app = Quart(__name__)
app.secret_key = os.getenv("APP_SECRET", default="1234567890")

conf = tk.config_from_environment()
cred = tk.Credentials(*conf)

token_spotify = tk.request_client_token(*conf[:2])
spotify = tk.Spotify(token_spotify, asynchronous=True)

taskset = set()
auths = {}

# region - logging
logging.basicConfig(
    level=logging.INFO,
    # format='%(asctime)s %(name)s %(module)s %(funcName)s %(levelname)s %(message)s',
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt="%Y-%m-%d %H:%M:%S"
    )

httpx_logger = logging.getLogger('httpx')
httpx_logger.setLevel(os.getenv("LOGLEVEL_HTTPX", default="WARN"))

# initial app running message
hypercorn_error = logging.getLogger("hypercorn.error")
hypercorn_error.disabled = True

# access log
hypercorn_access = logging.getLogger("hypercorn.access")
hypercorn_access.disabled = True

quart_logger = logging.getLogger('quart.app')
# quart_logger.basicConfig(
#     level=logging.INFO,
#     format='%(asctime)s %(name)s %(module)s %(funcName)s %(levelname)s %(message)s',
#     # format='%(asctime)s %(levelname)s %(message)s',
#     datefmt="%Y-%m-%d %H:%M:%S"
#     )
#endregion

# set up the async database connection
# as a part of Quart so it's easy
register_tortoise(
    app,
    db_url=os.environ['DATABASE_URL'],
    modules={"models": ["models"]},
    generate_schemas=False,
)

# if we need them, start some
# background tasks for users and queues
@app.before_serving
async def before_serving():
    """pre"""
    procname="before_serving"

    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager web_ui')
    logging.info("before_serving running tasks: %s", run_tasks)
    
    if "queue_manager" in run_tasks:
        logging.info("before_serving creating a queue manager task")
        qm = asyncio.create_task(queue_manager(spotify),name="queue_manager")
        taskset.add(qm)
        qm.add_done_callback(taskset.remove(qm))

    if "spotify_watcher" in run_tasks:
        
        if os.getenv("NOOVERTAKE") is not True:
            logging.warning("%s overtaking any existing watcher tasks", 
                        procname)    
            await User.select_for_update().exclude(watcherid='').update(watcherid='')

        logging.info("%s launching a user_reaper task", procname)
        reaper_task = asyncio.create_task(user_reaper(), name="user_reaper")
        taskset.add(reaper_task)
        reaper_task.add_done_callback(taskset.remove(reaper_task))
        
        # give the reaper a couple seconds to clean out inactive users
        await asyncio.sleep(2)
        
        logging.info("%s pulling active users for spotify watchers", procname)
        active_users = await User.filter(status="active")
        for user in active_users:
            await watchman(taskset, cred, spotify, spotify_watcher, user.spotifyid)


@app.before_request
def before_request():
    """save cookies even if you close your browser"""
    session.permanent = True


@app.route('/', methods=['GET'])
async def index():
    """show the now playing page"""
    procname="web_index"
    
    # get play history
    playhistory = await getrecents(spotify)
    
    # get a list of active user ids
    activeusers = await getactiveusers()
    displaynames = [x.displayname for x in activeusers]
    
    # check whether this is a known user or they need to login
    spotifyid = session.get('spotifyid', "login")
    if spotifyid == "" or spotifyid is None:
        spotifyid = "login"
        
        nid, _ = await getnext()
        trackname = await trackinfo(spotify, nid)

        # get outta here kid ya bother me
        return await render_template('index.html',
                                 spotifyid=spotifyid,
                                 np_name=trackname,
                                 activeusers=displaynames,
                                 history=playhistory
                                )
    
    # okay, we got a live one - get user details
    user, token = await getuser(cred, spotifyid)
    
    # fix any broken displaynames
    if user.displayname is None:
        user.displayname = "displayname unset"
        user.save()
    
    # followers should use their leader's token for
    # checking the currently-playing track, but nothing else
    if user.status.startswith("following"):
        logging.info("%s this user has user.status %s", procname, user.status)
        targetid = user.status.replace("following:", "")
        target = await User.get(displayname=targetid)
        currently_token = pickle.loads(target.token)
    else:
        currently_token = token
    
    # what's the player's current status?
    with spotify.token_as(currently_token):
        currently = await spotify.playback_currently_playing()

    # set some return values
    if currently is None or currently.is_playing is False:
        trackname="Not Playing"
        trackid = ""
        rating = ""
    else:
        trackid = currently.item.id
        trackname = await trackinfo(spotify, trackid)
        rating = await get_current_rating(trackid, activeusers=activeusers)

    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager')

    tasknames = [x.get_name() for x in asyncio.all_tasks()]

    if "spotify_watcher" in run_tasks and not user.status.startswith("following:"):
        if f"watcher_{spotifyid}" in tasknames:
            logging.debug("watcher_%s is running, won't start another", spotifyid)
        else:
            logging.info("no watcher for user, launching watcher_%s", spotifyid)
            await watchman(taskset, cred, spotify, spotify_watcher, spotifyid)

    if user.status.startswith("following"):
        return await render_template('index.html',
                                 spotifyid=spotifyid,
                                 displayname=user.displayname,
                                 np_name=trackname,
                                 np_id=trackid,
                                 rating=rating,
                                 activeusers=displaynames,
                                 history=playhistory
                                )
        
    return await render_template('index.html',
                                 spotifyid=spotifyid,
                                 displayname=user.displayname,
                                 np_name=trackname,
                                 np_id=trackid,
                                 rating=rating,
                                 activeusers=displaynames,
                                 history=playhistory
                                )


@app.route('/logout', methods=['GET'])
async def logout():
    """set a user as inactive and forget cookies"""
    spotifyid = request.cookies.get('spotifyid')
    if 'spotifyid' in session:
        spotifyid = session['spotifyid']
    else:
        return redirect("/")
    
    session['spotifyid'] = ""
    user = await User.get(spotifyid=spotifyid)
    user.status = "inactive"
    await user.save()
    return redirect("/")
    
    
@app.route('/auth', methods=['GET'])
async def spotify_authorization():
    """explain what's going on and redirect to spotify for authorization"""
    scope = [ "user-read-playback-state",
            "user-modify-playback-state",
            "user-read-currently-playing",
            "user-read-recently-played",
            "streaming",
            "app-remote-control",
            "user-library-read",
            "user-top-read",
            "user-follow-read",
            "playlist-read-private",
            ]
    auth = tk.UserAuth(cred, scope)
    logging.debug("auth=%s", auth)
    state = auth.state

    auths[state] = auth
    logging.debug("state: %s", state)
    logging.debug("auths: %s", auths)
    logging.debug("auth_url: %s", auth.url)
    
    trackid, _ = await getnext()
    trackname = await trackinfo(spotify, trackid)
    return await render_template('auth.html', 
                                 trackname=trackname,
                                 spoturl=auth.url)


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():
    """create a user record and set up initial ratings"""
    procname = "spotify_callback"

    # users = getactiveusers()
    code = request.args.get('code', "")
    state = request.args.get('state', "")
    logging.debug("state: %s", state)
    logging.debug("auths: %s", auths)
    thisauth = auths.pop(state, None)

    if thisauth is None:
        logging.error("%s couldn't find this state in auths: %s", procname, state)
        logging.error("auths: %s", auths)
        return redirect("/", 307)

    try:
        token = thisauth.request_token(code, state)
    except Exception as e:
        logging.error("%s exception request_token\n%s", procname, e)


    with spotify.token_as(token):
        try:
            spotify_user = await spotify.current_user()
        except Exception as e:
            logging.error("%s exception current_user\n%s", procname, e)
            return redirect("/")

    spotifyid = spotify_user.id
    session['spotifyid'] = spotifyid
    p = pickle.dumps(token)

    logging.info("spotify_callback get_or_create user record for %s", spotifyid)
    n = datetime.datetime.now(datetime.timezone.utc)
    user, created = await User.get_or_create(spotifyid=spotifyid,
                                             defaults={
                                                 "token": p,
                                                 "last_active": n,
                                                 "displayname": spotifyid,
                                                 "status": "active"
                                             })
    if created is False:
        logging.info('spotify_callback found user %s', spotifyid)
        user.last_active = datetime.datetime.now
        user.status = "active"
        await user.save()
    else:
        logging.info("spotify_callback creating new user %s", spotifyid)
        await user.save()             
        
        logging.info("spotify_callback pulling ratings to populate user")
        asyncio.create_task(pullratings(spotifyid),name=f"pullratings_{spotifyid}")
    
    logging.info("spotify_callback redirecting %s back to /", spotifyid)
    return redirect("/")


@app.route('/dash', methods=['GET'])
async def dashboard():
    """show what's happening"""
    # ratings = [x for x in Rating.select()]

    # queued = [await trackinfo(trackid) for trackid in queue]
    try:
        dbqueue = await UpcomingQueue.all().values_list('trackid', flat=True)
    except Exception as e: # pylint: disable=W0718
        logging.error("dashboard database queue retrieval exception: %s", e)

    tracknames = await Track.filter(trackid__in=dbqueue).values_list('trackname', flat=True)
    activeusers = await getactiveusers()
    history = await getrecents(spotify)
    tasknames = [x.get_name() for x in asyncio.all_tasks() if "Task-" not in x.get_name()]
    return await render_template('dashboard.html',
                                 auths=auths,
                                 username="now_playing",
                                 users=activeusers,
                                 tasks=tasknames,
                                 current=tracknames[0],
                                 nextup=tracknames[1],
                                 recents=history)


@app.route('/pullratings', methods=['GET'])
async def pullratings(spotifyid=None):
    """load up a bunch of ratings for a user"""
    if 'spotifyid' in session:
        spotifyid = session['spotifyid']
    else:
        return redirect("/auth")

    user, token = await getuser(cred, spotifyid)
    logging.debug("user=%s", user)

    with spotify.token_as(token):
        # try:
        #     ratings = Rating.get(user_id=spotifyid)
        # except Exception as e:
        #     logging.error("rating error: %s", e)

        # rate recent history (20 items)
        r = [item.track.id async for item in
             spotify.all_items(await spotify.playback_recently_played())]
        # recents = await spotify.playback_recently_played()
        rated = await rate_list(r, spotifyid, 1, set_last_played=False)

        # # rate tops
        # tops = await spotify.current_user_top_tracks()
        # tops = await spotify.all_items(await spotify.current_user_top_tracks())
        # rated = rated + await rate_list(tops, spotifyid, 4)

        saved_tracks = [item.track.id async for item in
                        spotify.all_items(await spotify.saved_tracks())]
        rated = rated + await rate_list(saved_tracks, spotifyid, 4, set_last_played=False)

        message = f"rated {rated} items"
        logging.info(message)

        return redirect("/")


@app.route('/user', methods=['POST'])
async def user_update():
    """allow user profile updates"""
    procname = "user_update"
    
    logging.info("%s updating user record", procname)
    
    if 'spotifyid' not in session:
        logging.info("%s no spotify id in session", procname)
        return redirect("/", 303)
    
    if request.method == "POST":
        logging.info("%s fetching user %s", 
                     procname, session['spotifyid'])
        user, _ = await getuser(cred, session['spotifyid'])
        
        form = await request.form
        displayname = form['displayname']
        logging.info("%s updating displayname: %s",
                     procname, displayname)
        if displayname is not None:
            logging.info("%s updating displayname: %s",
                         procname, displayname)
            user.displayname = displayname
            await user.save()
            
    return redirect("/")


@app.route('/user/impersonate/<userid>', methods=['GET'])
async def user_impersonate(userid):
    """act as somebody else"""
    procname = "user_impersonate"
    
    logging.warning("%s user impersonation: %s", procname, userid)
    session['spotifyid'] = userid
    
    return redirect("/")


@app.route('/follow/<targetid>')
async def follow(targetid=None):
    """listen with a friend"""
    procname = "follow"
    if 'spotifyid' in session:
        if session['spotifyid'] == targetid:
            logging.warning("%s user %s tried to follow itself",
                            procname, targetid)
        else:
            user, _ = await getuser(cred, session['spotifyid'])
            user.status = "following:" + targetid
            
            logging.info("%s set user %s status: %s", 
                         procname, session['spotifyid'], user.status)
            await user.save()
    return redirect("/")


async def main():
    """kick it"""
    secret=os.environ['SPOTIFY_CLIENT_SECRET']
    logging.debug("SPOTIFY_CLIENT_ID=%s", os.environ['SPOTIFY_CLIENT_ID'])
    logging.debug("SPOTIFY_CLIENT_SECRET=%s...%s", secret[-2:], secret[:2])
    logging.debug("SPOTIFY_REDIRECT_URI=%s", os.environ['SPOTIFY_REDIRECT_URI'])
    logging.info("main starting web_ui on port: %s", os.environ['PORT'])
    web_ui = app.run_task('0.0.0.0', os.environ['PORT'])
    taskset.add(web_ui)

    try:
        await asyncio.gather(*taskset)
    except Exception as e:
        logging.error("main gather exception %s", e)

    await asyncio.gather(*asyncio.all_tasks())
    logging.info("main done")


if __name__ == "__main__":
    asyncio.run(main())
