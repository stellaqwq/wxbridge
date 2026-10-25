def identity_scope(config):
    """Keep the existing private database identity; isolate group IDs and chat types."""
    if getattr(config, "chat_type", "friend") == "group":
        return config.self_id, "group", config.group_id, config.chat_name
    return config.self_id, config.peer_id, config.chat_name
