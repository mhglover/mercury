#!/usr/bin/env python
"""mercury radio"""
import asyncio
from datetime import timezone as tz, datetime as dt
import logging
import os
import pickle
import uuid

import tekore as tk
from dotenv import load_dotenv
from humanize import naturaltime
from quart import Quart, request, redirect, render_template, session
from tortoise.contrib.quart import register_tortoise

from models import User, WebData, PlayHistory, WebUser, Rating, Lock, WebTrack
from watchers import user_reaper, watchman, spotify_watcher
from users import getactiveusers, getuser, getactivewebusers
from queue_manager import queue_manager, getnext
from raters import rate_history, rate_saved, get_track_ratings
from raters import get_recent_playhistory_with_ratings, get_rating
from spot_funcs import trackinfo, getrecents, normalizetrack, get_webtrack
from helpers import feelabout

load_dotenv()  # take environment variables from .env

app = Quart(__name__)
app.config.from_prefixed_env()
app.secret_key = os.getenv("QUART_SECRET_KEY", default="1234567890")
app.instance_id = str(uuid.uuid4())
app.config.update(
    SESSION_COOKIE_SECURE=True,  # Ensure you also set Secure when setting SameSite=None
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',  # Can be 'Strict', 'Lax', or 'None'
)

conf = tk.config_from_environment()
cred = tk.Credentials(*conf)

token_spotify = tk.request_client_token(*conf[:2])
spotify = tk.Spotify(token_spotify, asynchronous=True)

# Global storage for asyncio tasks
taskset = set()

# Global used for passing authorization objects between the /auth and /spotify/callback routes
auths = {}

# region: logging configuration
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

#endregion


register_tortoise(
    # set up the async database connection as a part of Quart so it's easy
    app,
    db_url=os.environ['DATABASE_URL'],
    modules={"models": ["models"]},
    # get the value of the `GENERATE_SCHEMAS` environment variable
    generate_schemas=bool(os.environ.get('GENERATE_SCHEMAS', default="False"))
)


# region: app setup
@app.before_serving
async def before_serving():
    """
    This function is called before serving requests. 
    It performs several tasks based on the environment variables.

    Tasks:
    - If "queue_manager" is in the `RUN_TASKS` environment variable it creates a queue manager task.
    - If "spotify_watcher" is in the `RUN_TASKS` env var, it performs the following steps:
        - Updates the `watcherid` field of all users to an empty string.
        - Launches a user_reaper task.
        - Pulls active users for spotify watchers.
        - Calls the `watchman` function for each active user.
        - Retrieves upcoming recommendations and logs them.
    
    """
    procname="before_serving"
    
    #if the ERASE_LOCKS env var is set, delete all locks
    if os.getenv('ERASE_LOCKS', default=None) == "True":
        logging.warning("%s erasing all locks", procname)
        await Lock.all().delete()    

    # check to see which tasks we're supposed to be running on this instance
    run_tasks = os.getenv('RUN_TASKS', 'spotify_watcher queue_manager web_ui')
    logging.debug("before_serving running tasks: %s", run_tasks)
    
    if "queue_manager" in run_tasks:
        logging.debug("%s creating a queue manager task", procname)
        qm = asyncio.create_task(queue_manager(spotify),name="queue_manager")
        taskset.add(qm)
        qm.add_done_callback(taskset.remove(qm))

    if "spotify_watcher" in run_tasks:
        
        await User.select_for_update().exclude(watcherid='').update(watcherid='')

        logging.debug("%s launching a user_reaper task", procname)
        reaper_task = asyncio.create_task(user_reaper(), name="user_reaper")
        taskset.add(reaper_task)
        reaper_task.add_done_callback(taskset.remove(reaper_task))
        
        logging.debug("%s pulling active users for spotify watchers", procname)
        active_users = await getactiveusers()
        for user in active_users:
            await watchman(taskset, cred, spotify, spotify_watcher, user)
        
        nextup = await getnext(get_all=True)
        for track in nextup:
            logging.info("upcoming recommendation: [%s] %s (%s)", 
                         track.reason, track.trackname, naturaltime(track.expires_at))
        
        logging.debug("%s ready", procname)


@app.before_request
def before_request():
    """save cookies even if you close your browser"""
    session.permanent = True
# endregion


@app.route('/', methods=['GET'])
async def index():
    """show the now playing page"""
    
    # check whether this is a known user or they need to login
    user_id = session.get('user_id', None)
    
    nextup = await getnext(webtrack=True)
    
    # what's happening y'all
    web_data = WebData(
        nextup=nextup,
        ratings=[]
        )
    
    # go ahead and return if this isn't an active user
    if user_id is None or user_id == '':
        web_data.history = await get_recent_playhistory_with_ratings(2)
        # get outta here kid ya bother me
        return await render_template('index.html', w=web_data.to_dict())
    
    # okay, we got a live one - get user details
    web_data.user, token = await getuser(cred, user_id)
    web_data.nextup = await getnext(webtrack=True, user=web_data.user)
    
    if web_data.nextup:
        web_data.users = await getactivewebusers(web_data.nextup.track_id)
    
    web_data.history = await get_recent_playhistory_with_ratings(web_data.user.id)
    
    # what's the player's current status?
    with spotify.token_as(token):
        currently = await spotify.playback_currently_playing()

    # set some return values
    if currently is not None:
        web_data.refresh = ((currently.item.duration_ms - currently.progress_ms) // 1000) +1
        track = await trackinfo(spotify, currently.item.id)
        web_data.track = await get_webtrack(track, web_data.user)
        try:
            web_data.users = await getactivewebusers(track)
        except Exception as e:
            logging.error("index - getactivewebusers\n%s", e)
        
        # web_data.rating = await get_current_rating(
            # web_data.track, activeusers=web_data.activeusers)

    if web_data.user.status == "active":
        # see if we need to launch a task for this user
        await watchman(taskset, cred, spotify, spotify_watcher, web_data.user)
    
    
    # let's see it then
    return await render_template('index.html', w=web_data.to_dict())


@app.route('/tunein')
async def tunein():
    """if necessary, remotely start the player and start a spot_watcher"""
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect(request.referrer)
    
    user, token = await getuser(cred, user_id)
    with spotify.token_as(token):
        currently = await spotify.playback_currently_playing()
    
    nextup = await getnext()
    user.status = "active"
    logging.info("user tuning in - %s", user.displayname)
    await user.save()
    
    if (nextup and currently and currently.is_playing is False):
        with spotify.token_as(token):
            logging.info("playback_start_tracks - %s - %s",
                         user.displayname, nextup.track.trackname)
            try:
                await spotify.playback_start_tracks([nextup.track.spotifyid])
            except tk.NotFound as e:
                logging.error(
                    "web_listen - no active player for user %s, can't send track to player\n%s",
                    user.displayname, e)
    
    await watchman(taskset, cred, spotify, spotify_watcher, user)
    
    # check the user's recent history for unrated tracks
    await rate_history(spotify, user, token, limit=50)


    return redirect(request.referrer)


@app.route('/tuneout')
async def tuneout():
    """stop spotwatcher, go inactive"""
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect(request.referrer)
    
    user, _ = await getuser(cred, user_id)
    user.status = "inactive"
    await user.save()
    logging.info("user tuning out - %s - %s", user.displayname, user.status)

    return redirect(request.referrer)


@app.route('/logout', methods=['GET'])
async def logout():
    """set a user as inactive and forget cookies"""
    
    # check for the session credentials and unset them
    user_id = session.get('user_id', None)
    session['user_id'] = ""
    
    if user_id:
        user = await User.get(id=user_id)
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
    
    nextup=await getnext(webtrack=True)
    w = WebData()
        
    return await render_template('auth.html',
                                 w=w,
                                 trackname=nextup.trackname,
                                 spoturl=auth.url)


@app.route('/spotify/callback', methods=['GET','POST'])
async def spotify_callback():
    """create a user record and set up initial ratings"""
    procname = "spotify_callback"

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

    p = pickle.dumps(token)

    logging.info("%s get_or_create user record for %s", procname, spotifyid)
    user, created = await User.get_or_create(spotifyid=spotifyid,
                                             defaults={
                                                 "token": p,
                                                 "last_active": dt.now(tz.utc),
                                                 "displayname": spotify_user.display_name,
                                                 "status": "active"
                                             })
    if created is False:
        logging.info('%s found user %s', procname, spotifyid)
        user.last_active = dt.now(tz.utc)
        user.status = "active"
        await user.save()
    else:
        logging.info("%s creating new user %s", procname, spotifyid)
        await user.save()
        
        logging.info("%s pulling ratings to populate user", procname)
        asyncio.create_task(pullratings(user.id), name=f"pullratings_{spotifyid}")
    
    session['user_id'] = user.id
    logging.info("%s redirecting %s back to /", procname, spotifyid)
    return redirect("/")


@app.route('/dash', methods=['GET'])
async def dashboard():
    """show what's happening"""
    
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    
    if user.role != "admin":
        return redirect("/")

    # what's happening y'all
    data = WebData(
        history=await getrecents(),
        users = await getactiveusers(),
        nextup = await getnext(webtrack=True, user=user),
        ratings=[]
        )
    
    return await render_template('dashboard.html', w=data)


@app.route('/pullratings', methods=['GET'])
async def pullratings(user_id=None):
    """load up a bunch of ratings for a user"""
    
    if not user_id:
        user_id = session.get('user_id', None)
    
    if not user_id:
        return redirect("/")
    
    user, token = await getuser(cred, user_id)

    with spotify.token_as(token):

        # rate recent history (20 items)
        await rate_history(spotify, user, token)
        
        # rate saved tracks
        await rate_saved(spotify, user, token)
        
        # tops = await spotify.all_items(await spotify.current_user_top_tracks())

        return redirect("/")


@app.route('/user/<target_id>', methods=['GET', 'POST'])
async def web_user(target_id):
    """allow user profile updates"""
    procname = "web_user"
    
    user_id = session.get('user_id', None)
    
    if not user_id:
        logging.info("%s no user_id in session", procname)
        return redirect("/", 303)
    
    user, _ = await getuser(cred, user_id)
    target = await User.get_or_none(id=target_id)
    
    if not target:
        return redirect("/")
    
    u = WebUser(
        displayname=target.displayname,
        user_id=target.id
    )
    w = WebData(user=user)
    
    # get all the user's ratings
    ratings = await Rating.filter(user_id=target.id).order_by('trackname').prefetch_related("track")
    
    w.ratings = [WebTrack(trackname=r.trackname,
                  track_id=r.track_id,
                  comment=r.comment if r.rating else "",
                  color=feelabout(r.rating),
                  rating=r.rating) for r in ratings]
    
    if request.method == "GET":
        return await render_template("user.html", w=w, u=u)
    
    if request.method == "POST":
        logging.info("%s updating user record", procname)
        logging.info("%s fetching user %s", 
                     procname, user.id)
        
        form = await request.form
        displayname = form['displayname']
        logging.info("%s updating displayname: %s",
                     procname, displayname)
        if displayname is not None:
            logging.info("%s updating displayname: %s",
                         procname, displayname)
            user.displayname = displayname
            await user.save()
        
        return redirect(request.referrer)
    
    return redirect("/")


@app.route('/user/<user_id>/nuke')
async def nuke_me(user_id):
    """delete all records associated with a user"""
    procname = "nuke_me"
    user_id = session.get('user_id', None)
    
    if not user_id:
        logging.info("%s no user_id in session", procname)
        return redirect(request.referrer)
    
    user, _ = await getuser(cred, user_id)
    
    logging.warning("%s nuking user %s", procname, user.displayname)
    PlayHistory.filter(user_id=user.id).delete()
    Rating.filter(user_id=user.id).delete()
    user.delete()


@app.route('/user/<target_id>/impersonate', methods=['GET'])
async def user_impersonate(target_id):
    """act as somebody else"""
    procname = "user_impersonate"
    
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    
    if "admin" not in user.role:
        return redirect("/")
    
    logging.warning("%s admin user %s impersonation: %s", procname, user.displayname, target_id)
    session['user_id'] = target_id
    
    return redirect("/")


@app.route('/user/<target_id>/follow')
async def follow(target_id):
    """listen with a friend"""
    procname = "follow"
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect(request.referrer)
    
    user, _ = await getuser(cred, user_id)
    
    if session['user_id'] == target_id:
        logging.warning("%s user %s tried to follow itself", procname, user_id)
        return redirect(request.referrer)

    user.status = "following"
    user.watcherid = target_id
    await user.save()
    
    logging.info("%s updated user %s - status: %s - watcherid: %s", 
                         procname, user_id, user.status, user.watcherid)
    return redirect(request.referrer)


@app.route('/track/<track_id>', methods=['GET', 'POST'])
async def web_track(track_id):
    """display/edit track details"""
    
    # get the details we need to show
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    
    track = await normalizetrack(track_id)
    
    if request.method == "POST":
        now = dt.now(tz.utc)
        rating, _ = await Rating.get_or_create(user_id=user.id,
                                                track_id=track.id,
                                                trackname=track.trackname,
                                                defaults={
                                                   "rating": 0,
                                                   "last_played": now
                                                   }
                                               )
        form = await request.form
        rating.comment = form['comment']
        await rating.save()
    
    webtrack = await get_webtrack(track, user=user)
    
    # nextup = await getnext(webtrack=True, user=user)
    
    ratings = await get_track_ratings(track)
    
    # what's happening y'all
    w = WebData(
        user = user,
        track = webtrack,
        ratings = ratings,
        # nextup = nextup,
        refresh = 0
        )
    
    ph = await PlayHistory.filter(track_id=track.id).order_by('-played_at').prefetch_related("user")
    history = [f"{x.user.displayname} - {naturaltime(x.played_at)}" for x in ph] 
    
    return await render_template('track.html', history=history, w=w.to_dict())


@app.route('/track/<track_id>/rate/<action>', methods=['GET'])
async def web_rate_track(track_id, action):
    """rate a track"""
    
    user_id = session.get('user_id', None)
    if not user_id:
        return redirect("/")
    
    if action not in ['up', 'down']:
        return redirect("/")
    
    user, _ = await getuser(cred, user_id)
    
    track = await normalizetrack(track_id)
    
    rating = await get_rating(user, track.id)

    rating.rating = int(rating.rating) + 1 if action == 'up' else int(rating.rating) - 1
    await rating.save()
    
    return redirect(request.referrer)



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
