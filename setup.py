import os
from setuptools import setup

with open("requirements.txt") as f:
    install_requires = [line.strip() for line in f if line.strip() and not line.startswith("#")]

readme_path = os.path.join(os.path.dirname(__file__), "README.md")
if os.path.exists(readme_path):
    with open(readme_path) as f:
        long_description = f.read()
    long_description_content_type = "text/markdown"
else:
    long_description = ""
    long_description_content_type = "text/plain"

setup(
    name="ddtui",
    version="0.1.0",
    description="DeepSeek thinking-mode TUI agent built on Textual",
    long_description=long_description,
    long_description_content_type=long_description_content_type,
    author="ddddwee1",
    url="https://github.com/ddddwee1/dd_agent_tui",
    packages=["ddtui"],
    py_modules=["agent"],   # shim so `python agent.py` and `from agent import main` keep working
    install_requires=install_requires,
    python_requires=">=3.10",
    entry_points={
        "console_scripts": [
            "ddtui=ddtui.app:main",
        ],
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Environment :: Console",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
    ],
)
