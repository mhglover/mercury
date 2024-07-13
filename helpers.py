# helpers.py
"""assistive functions"""

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

def truncate_middle(s, n=30):
    """shorten long names"""
    if len(s) <= n:
        # string is already short-enough
        return s
    if n <= 3:
        return s[:n] # Just return the first n characters if n is very small
     # half of the size, minus the 3 .'s
    n_2 = (n - 3) // 2
    # whatever's left
    n_1 = n - n_2 - 3
    return '{0}...{1}'.format(s[:n_1], s[-n_2:]) 


def feelabout(value: int):
    """return a text string based on value"""
    return USER_RATINGS_TO_FEELINGS.get(value)
