"""functions for user manipulations"""
import logging
import pickle
from pprint import pformat
import tekore as tk
from helpers import feelabout
from models import Track, User, WebUser

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace, trailing-newlines

# used by feelabout()

async def getuser(cred, user):
    """fetch user details
    cred - spotify credentials object
    userid - either a user object, a user.id, or a user.spotifyid
    
    returns: user object, spotify token
    """
    
    # if it's a string, it'll be a user's spotifyid, so fetch the User
    if isinstance(user, str):
        try:
            user = await User.get(spotifyid=user)
        except Exception as e:
            logging.error("getuser exception 1 fetching user\n%s", e)
    
    # if it's an int, it'll be a User record id, so fetch the User
    elif isinstance(user, int):
        try:
            user = await User.get(id=user)
        except Exception as e:
            logging.error("getuser exception 2 fetching user\n%s", e)
    
    # if it's not a User by now, it's broken.
    if not isinstance(user, User):
        logging.error("getuser unable to find user %s", user)
        user = None
        token = None
    else:
        # pull the latest saved token
        token = pickle.loads(user.token)
        
        # renew it if necessary
        if token.is_expiring:
            try:
                token = cred.refresh(token)
            except Exception as e:
                logging.error("getuser exception refreshing token\n%s", e)
            
            # save the new token
            user.token = pickle.dumps(token)
            await user.save()
    
    # return the user object and an unpickled token
    return user, token


async def getactiveusers():
    """fetch details for the active users
    
    returns: list of Users
    """
    return await User.exclude(status="inactive")


async def getactivewebusers(track):
    """Fetch users and ratings for a Track
    
    Returns: list of Webusers   
    """
    activeusers = await getactiveusers()
    logging.debug("activeusers: %s", activeusers)
    
    track = await Track.filter(id=track.id).prefetch_related("ratings").get()
    
    # dict comprehension to create a ratings map
    user_ratings = {rating.user_id: rating.rating for rating in track.ratings}
    logging.debug("user_ratings: %s", user_ratings)

    webusers = [
        WebUser(
            displayname=user.displayname,
            user_id=user.id,
            rating=feelabout(user_ratings.get(user.id)),
            track_id=track.id,
            trackname=track.trackname
        )
        for user in activeusers
    ]
    
    return webusers


async def getplayer(state):
    """check the current player stat and update user status"""
    # move into models.WatcherState?
    procname = "users.getplayer"
    logging.debug("%s checking currently playing", procname)
    with state.spotify.token_as(state.token):
        try:
            currently = await state.spotify.playback_currently_playing()
        except tk.Unauthorised as e:
            state.status = "unauthorized"
            logging.debug("%s unauthorized access from spotify player\n%s", procname, e)
            return
            
        except Exception as e:
            logging.error("%s exception in spotify.playback_currently_playing\n%s",procname, e)
            return

    if state.user.status != "active":
        state.status = state.user.status
        logging.info("%s player is not active, sleeping: %s", procname, state.user.status)
        
    # is it not playing?
    if currently is None:
        state.user.status = "not playing"
        state.status = "not playing"
        logging.debug("%s not currently playing", procname)

    # not playing but not paused?  weird state
    elif currently.currently_playing_type == "unknown":
        state.user.status = "not playing"
        state.status = "not playing"
        logging.debug("%s not currently playing", procname)
        raise ValueError(f"currently_playing_type says 'unknown'\n{currently}")

    # paused
    elif currently.is_playing is False:
        state.user.status = "paused"
        state.status = "paused"
        logging.debug("%s is paused", procname)
    
    # must be active then
    else:
        state.status = "active"
        state.user.status = "active"
        logging.debug("%s is active", procname)
    
    await state.user.save()
    return currently
        