# TrailCurrent Peregrine — branded shell prompt and aliases
# Sourced by /etc/profile via /etc/profile.d/

if [ -n "${BASH_VERSION:-}" ] && [ -t 0 ]; then
    PS1='\[\033[38;5;70m\]trail\[\033[38;5;30m\]current\[\033[0m\]@\[\033[38;5;70m\]\h\[\033[0m\]:\w\$ '
fi

# Convenience aliases
alias peregrine-logs='sudo journalctl -u voice-assistant -f'
alias peregrine-genie-logs='sudo journalctl -u genie-server -f'
alias peregrine-restart='sudo systemctl restart voice-assistant'
alias peregrine-status='systemctl status voice-assistant genie-server --no-pager'
alias peregrine-self-test='/usr/local/bin/peregrine-self-test.sh'
