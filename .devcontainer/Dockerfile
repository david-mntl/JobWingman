# Dev container image for JobWingman Python service.
# Separate from python-service/Dockerfile (production) — this one includes
# dev tooling and runs as a non-root user so file permissions work cleanly
# with the VS Code devcontainer extension.

FROM python:3.12-slim

# Install system packages needed for development
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    make \
    zsh \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root user that matches the VS Code devcontainer convention
ARG USERNAME=vscode
ARG USER_UID=1000
ARG USER_GID=1000
RUN groupadd --gid $USER_GID $USERNAME \
    && useradd --uid $USER_UID --gid $USER_GID -m $USERNAME --shell /bin/zsh

# Install Oh My Zsh for a better terminal experience
RUN sh -c "$(curl -fsSL https://raw.githubusercontent.com/ohmyzsh/ohmyzsh/master/tools/install.sh)" "" --unattended

# Copy Oh My Zsh to the vscode user's home and fix ownership
RUN cp -r /root/.oh-my-zsh /home/$USERNAME/.oh-my-zsh \
    && cp /root/.zshrc /home/$USERNAME/.zshrc \
    && sed -i "s|/root|/home/$USERNAME|g" /home/$USERNAME/.zshrc \
    && chown -R $USERNAME:$USERNAME /home/$USERNAME/.oh-my-zsh /home/$USERNAME/.zshrc

WORKDIR /workspace

# Install Python dependencies ahead of copying source so Docker can cache
# this layer — only rebuilds when requirements.txt changes
COPY ../python-service/requirements.txt ./python-service/requirements.txt
RUN pip install --no-cache-dir -r python-service/requirements.txt

USER $USERNAME
