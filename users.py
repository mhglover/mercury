"""functions for user manipulations"""
import logging
import pickle
import tekore as tk
from models import User, WebUser, Track

# pylint: disable=broad-exception-caught
# pylint: disable=trailing-whitespace

# used by feelabout()
USER_RATINGS_TO_FEELINGS = {
    None:   "unrated",
    -2:     "hate",
    -1:     "dislike",
     0:     "shrug",
     1:     "like",
     2:     "love",
     3:     "love",
     4:     "love"
}

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

def feelabout(value: int):
    """return a text string based on value"""
    return USER_RATINGS_TO_FEELINGS.get(value)


async def getplayer(spotify, user):
    """check the current player stat and update user status"""
    procname = "users.getplayer"
    try:
        currently = await spotify.playback_currently_playing()
    except tk.Unauthorised as e:
        logging.error("%s unauthorized access - renew token?\n%s",procname, e)
        return 401
    except Exception as e:
        logging.error("%s exception in spotify.playback_currently_playing\n%s",procname, e)
        return

    # is it not playing?
    if currently is None:
        user.status = "not playing"
        logging.debug("%s not currently playing", procname)

    # not playing but not paused?  weird state
    elif currently.currently_playing_type == "unknown":
        user.status = "not playing"
        logging.debug("%s not currently playing", procname)
        raise ValueError(f"currently_playing_type says 'unknown'\n{currently}")

    # paused
    elif currently.is_playing is False:
        user.status = "paused"
        logging.debug("%s is paused", procname)
    
    # must be active then
    else:
        user.status = "active"
    
    await user.save()
    return currently
        