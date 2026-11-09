def check(user_id):
    if user_id <= 0:
        raise ValueError("bad id")
    return True
