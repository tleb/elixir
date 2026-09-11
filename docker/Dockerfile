FROM debian:forky

RUN \
  apt-get update && \
  apt-get --no-install-recommends -y install \
    python3 \
    python3-pip \
    python3-venv \
    git \
    apache2 \
    libapache2-mod-wsgi-py3 \
    universal-ctags \
    wget

COPY ./requirements.txt /usr/local/elixir/

WORKDIR /usr/local/elixir/

RUN python3 -m venv venv && \
    . ./venv/bin/activate && \
    pip install -r requirements.txt

RUN mkdir -p /srv/elixir-data/

COPY ./docker/000-default.conf /etc/apache2/sites-available/000-default.conf
COPY ./docker/gitconfig /etc/gitconfig

RUN a2enmod rewrite

EXPOSE 80

ARG ELIXIR_VERSION

ENV ELIXIR_VERSION=$ELIXIR_VERSION \
    ELIXIR_ROOT=/srv/elixir-data \
    PATH="/usr/local/elixir/utils:/usr/local/elixir/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

COPY . /usr/local/elixir/

ENTRYPOINT ["/usr/sbin/apache2ctl", "-D", "FOREGROUND"]
