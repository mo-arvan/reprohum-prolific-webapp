# Source: https://github.com/docker/awesome-compose/tree/master/flask
FROM python:3.12.1-alpine3.19 AS builder

WORKDIR /app

COPY requirements.txt /app
RUN pip3 install -r requirements.txt

COPY . /app

#ENTRYPOINT ["python3"]
#RUN python3 CreateDatabase.py
CMD ["python3", "main.py"]