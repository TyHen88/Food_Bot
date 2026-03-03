#!/usr/bin/env python3
"""
Setup script for the Telegram Food Poll Bot.
"""

from setuptools import setup, find_packages

# Read the README file
def read_readme():
    with open("README.md", "r", encoding="utf-8") as fh:
        return fh.read()

# Read requirements
def read_requirements():
    with open("requirements.txt", "r", encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="telegram-food-poll-bot",
    version="1.0.0",
    author="Henty",
    author_email="[EMAIL_ADDRESS]",
    description="A Telegram bot that creates interactive polls from food menu text",
    long_description=read_readme(),
    long_description_content_type="text/markdown",
    url="https://github.com/TyHen88/Food_Bot.git",   
    packages=find_packages(),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Communications :: Chat",
        "Topic :: Software Development :: Libraries :: Python Modules",
    ],
    python_requires=">=3.8",
    install_requires=read_requirements(),
    entry_points={
        "console_scripts": [
            "food-poll-bot=main:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
    keywords="telegram bot food poll menu ordering",
    # project_urls={
    #     "Bug Reports": "https://github.com/yourusername/foot-auto-poll-bot/issues",
    #     "Source": "https://github.com/yourusername/foot-auto-poll-bot",
    #     "Documentation": "https://github.com/yourusername/foot-auto-poll-bot#readme",
    # },
) 