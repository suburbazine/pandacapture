# Build environment for firmware/build.py --docker
FROM ubuntu:24.04
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip git ca-certificates \
 && rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages --no-cache-dir scons comma-deps-gcc-arm-none-eabi
# The repository is mounted at /src, owned by another user
RUN git config --system --add safe.directory '*'
