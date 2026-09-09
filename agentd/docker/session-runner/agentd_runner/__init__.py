"""In-container agentd-runner — PID 1, JSON-RPC supervisor (§7, §14)."""

# Tracks the deployed image tag (`supervisor.DEFAULT_IMAGE`), because the runner
# *is* the image: there is no way to install a version of this package other
# than by building a tag. Held to that tag by
# `tests/test_runner_image_isolation.py::test_runner_package_version_tracks_the_deployed_tag`,
# added after this string sat at 1.0.0 across four deploys while the ops notes
# discussed "Runner 1.4.0" — nothing read it, so nothing caught it.
__version__ = "1.5.0"
