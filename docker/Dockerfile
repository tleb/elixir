FROM debian:forky

RUN \
  apt-get update && \
  apt-get --no-install-recommends -y install \
    python3 \
    ca-certificates \
    git \
    apache2 \
    libapache2-mod-wsgi-py3 \
    universal-ctags \
    wget

# uv, pinned to a tag of ghcr.io/astral-sh/uv
COPY --from=ghcr.io/astral-sh/uv:0.12.13 /uv /uvx /usr/local/bin/

# Use the system Python installed above, never download a uv-managed one
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /usr/local/elixir/

# Dependency layer: only pyproject.toml and uv.lock are needed, so this
# stays cached between source-only edits. The project itself is left out
# (--no-install-project) until its tree is copied below. The dev group
# is included too: the image doubles as the documented test runner
# (`docker run --entrypoint pytest elixir t/`), like the old image that
# carried pytest in requirements.txt.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-cache \
    --compile-bytecode

RUN mkdir -p /srv/elixir-data/

COPY ./docker/000-default.conf /etc/apache2/sites-available/000-default.conf
COPY ./docker/gitconfig /etc/gitconfig

RUN a2enmod rewrite

# The project installs editable into .venv: the full source tree must
# live at /usr/local/elixir in the image, not just the package, because
# elixir.web resolves templates/ and static/ relative to it (CURRENT_DIR
# in elixir/lib.py). This second sync only adds the project itself.
COPY . /usr/local/elixir/
RUN uv sync --frozen --no-cache --compile-bytecode

EXPOSE 80

ARG ELIXIR_VERSION

ENV ELIXIR_VERSION=$ELIXIR_VERSION \
    PATH="/usr/local/elixir/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/sbin/apache2ctl", "-D", "FOREGROUND"]
