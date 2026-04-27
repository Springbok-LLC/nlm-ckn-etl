#!/usr/bin/env bash
db="Cell-KN-Phenotypes"
container_id=$(docker ps | grep fuseki | cut -d " " -f 1)
if [ -z "$container_id" ]; then
    docker run \
           --platform linux/amd64 \
           --name fuseki \
           --restart always \
           -e ADMIN_PASSWORD=$JENA_PASSWORD \
           -e TDB=2 \
           -e FUSEKI_DATASET_1=$db \
           -p 3030:3030 \
           -d \
           -v $JENA_HOME:/fuseki \
           stain/jena-fuseki:latest > /dev/null
fi
