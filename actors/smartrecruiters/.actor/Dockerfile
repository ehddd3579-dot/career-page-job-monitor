FROM apify/actor-python:3.12

COPY requirements.txt ./

RUN echo "Python version:" \
 && python --version \
 && echo "Installing dependencies:" \
 && pip install --no-cache-dir -r requirements.txt \
 && echo "All installed Python packages:" \
 && pip freeze

COPY . ./

RUN python -c "import src.main" \
 && echo "Compilation check passed."

CMD ["python3", "-m", "src"]

