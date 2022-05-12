# mercury
discord based spotify queue manager

## environment setup

    brew tap heroku/brew && brew install heroku
    brew install pyenv
    brew install pip
    
    echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.bashrc
    echo 'command -v pyenv >/dev/null || export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.bashrc
    echo 'eval "$(pyenv init -)"' >> ~/.bashrc
    exec $SHELL
    
    pyenv install 3.10.4

    brew install pyenv-virtualenv
    pyenv virtualenv 3.10.4 mercury 
    pyenv local mercury
    pip config set global.disable-pip-version-check true
    pip install -r requirements.txt
    heroku auth:login
    git remote add heroku git@heroku.com:heroku.git
