"""The invoking profile's preference for automatic CLI wrapper creation."""


def skip_automatic_profile_alias(no_alias: bool = False) -> bool:
    """An explicit flag wins; otherwise only YAML ``false`` opts out."""
    if no_alias:
        return True
    from hermes_cli.config_effective import load_user_config_effective

    settings = load_user_config_effective().get("profiles")
    return isinstance(settings, dict) and settings.get("auto_alias") is False
