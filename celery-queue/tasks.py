# Copyright 2020 by Patrik Jonell.
# All rights reserved.
# This file is part of the GENEA visualizer,
# and is released under the GPLv3 License. Please see the LICENSE
# file that should have been included as part of this package.


import os
from celery import Celery
import subprocess
from celery.utils.log import get_task_logger
import requests
import tempfile
from pyvirtualdisplay import Display
from bvh import Bvh

Display().start()


logger = get_task_logger(__name__)


WORKER_TIMEOUT = int(os.environ["WORKER_TIMEOUT"])
celery = Celery(
    "tasks",
    broker=os.environ["CELERY_BROKER_URL"],
    backend=os.environ["CELERY_RESULT_BACKEND"],
)


class TaskFailure(Exception):
    pass


def validate_bvh_file(bvh_file):
    MAX_NUMBER_FRAMES = int(os.environ["MAX_NUMBER_FRAMES"])
    FRAME_TIME = 1.0 / float(os.environ["RENDER_FPS"])

    file_content = bvh_file.decode("utf-8")
    mocap = Bvh(file_content)
    counter = None
    for line in file_content.split("\n"):
        if counter is not None and line.strip():
            counter += 1
        if line.strip() == "MOTION":
            counter = -2

    if mocap.nframes != counter:
        raise TaskFailure(
            f"The number of rows with motion data ({counter}) does not match the Frames field ({mocap.nframes})"
        )

    if MAX_NUMBER_FRAMES != -1 and mocap.nframes > MAX_NUMBER_FRAMES:
        raise TaskFailure(
            f"The supplied number of frames ({mocap.nframes}) is bigger than {MAX_NUMBER_FRAMES}"
        )

    if mocap.frame_time != FRAME_TIME:
        raise TaskFailure(
            f"The supplied frame time ({mocap.frame_time}) differs from the required {FRAME_TIME}"
        )


@celery.task(name="tasks.render", bind=True, hard_time_limit=WORKER_TIMEOUT)
def render(self, bvh_file_uri: str) -> str:
    HEADERS = {"Authorization": f"Bearer " + os.environ["SYSTEM_TOKEN"]}
    API_SERVER = os.environ["API_SERVER"]

    logger.info("rendering..")
    self.update_state(state="PROCESSING")

    bvh_file = requests.get(API_SERVER + bvh_file_uri, headers=HEADERS).content
    validate_bvh_file(bvh_file)

    with tempfile.NamedTemporaryFile(suffix=".bhv") as tmpf:
        tmpf.write(bvh_file)
        tmpf.seek(0)

        process = subprocess.Popen(
            [
                "/blender/blender-2.83.0-linux64/blender",
                "-noaudio",
                "-b",
                "--python",
                "blender_render.py",
                "--",
                tmpf.name,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        total = None
        current_frame = None
        for line in process.stdout:
            line = line.decode("utf-8").strip()
            if line.startswith("total_frames "):
                _, total = line.split(" ")
                total = int(float(total))
            elif line.startswith("Append frame "):
                *_, current_frame = line.split(" ")
                current_frame = int(current_frame)
            elif line.startswith("output_file"):
                _, file_name = line.split(" ")
                files = {"file": (os.path.basename(file_name), open(file_name, "rb"))}
                return requests.post(
                    API_SERVER + "/upload_video", files=files, headers=HEADERS
                ).text
            if total and current_frame:
                self.update_state(
                    state="RENDERING", meta={"current": current_frame, "total": total}
                )
        if process.returncode != 0:
            raise TaskFailure(process.stderr.read().decode("utf-8"))
