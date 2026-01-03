if [-d "venv"]; then
    source venv/bin/activate
else
    python -m venv .venv
    source .venv/bin/activate
fi 

pip install -r requirements.txt