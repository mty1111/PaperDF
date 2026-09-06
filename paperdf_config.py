"""Explicit configuration loading shared by desktop and command-line entrypoints."""
import configparser
import os
from pathlib import Path
from dotenv import dotenv_values

CONFIG_FILENAME = 'pdf_metadata_renamer.config'
ENV_FILENAME = 'pdf_metadata_renamer.env'


def default_store_dir():
    return Path(os.getenv('LOCALAPPDATA', os.path.expanduser('~'))) / 'pdfrenamer'


def load_config(directory):
    """Read settings without creating directories or changing process environment."""
    directory = Path(directory)
    config = configparser.ConfigParser(interpolation=None)
    path = directory / CONFIG_FILENAME
    if path.exists():
        try:
            config.read(path, encoding='utf-8')
        except UnicodeDecodeError:
            config.read(path)
    if 'Settings' not in config:
        config['Settings'] = {}
    return config


def api_key_for(directory, settings):
    # Preserve desktop precedence: saved setting, environment, then local dotenv.
    if 'api_key' in settings:
        return settings['api_key']
    if 'GEMINI_API_KEY' in os.environ:
        return os.environ['GEMINI_API_KEY']
    path = Path(directory) / ENV_FILENAME
    return (dotenv_values(path).get('GEMINI_API_KEY') or '') if path.exists() else ''
