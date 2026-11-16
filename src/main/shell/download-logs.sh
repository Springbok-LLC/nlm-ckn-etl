log_group_name="/batch/nlm-ckn-release"
log_stream_name="$1"

echo ${log_group_name}
echo ${log_stream_name}

aws logs filter-log-events \
    --log-group-name ${log_group_name} \
    --log-stream-names ${log_stream_name} \
    --output json \
    | jq -r '.events[].message' \
    > nlm-ckn-etl-log.txt
