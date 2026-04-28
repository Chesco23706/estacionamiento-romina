from functools import wraps

from flask import abort
from flask_login import current_user


def role_required(*role_names):
    def decorator(view):
        @wraps(view)
        def wrapped_view(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)

            if current_user.role is None or current_user.role.name not in role_names:
                abort(403)

            return view(*args, **kwargs)

        return wrapped_view

    return decorator
