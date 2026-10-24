# The binary is built on the host (scripts/build-app.sh), so the image needs
# no base layers and no registry pull.
FROM scratch
COPY mttr-app /mttr-app
USER 65534:65534
EXPOSE 8080
ENTRYPOINT ["/mttr-app"]
