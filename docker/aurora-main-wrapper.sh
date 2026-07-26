#!/command/with-contenv sh
# Custom Aurora image entry wrapper: fail closed on image identity before
# starting the upstream command router.
set -eu
/opt/aurora/startup-check.sh
exec /opt/hermes/docker/main-wrapper.sh "$@"
