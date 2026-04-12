
# TrailCurrent Peregrine — first-login wizard
# Runs once on the first interactive shell, then never again.
if [ ! -f "$HOME/.peregrine-setup-complete" ] && [ -t 0 ]; then
    /usr/local/bin/peregrine-first-login.sh
fi
