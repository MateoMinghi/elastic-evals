package eeopensearchexporter

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/url"
	"strings"

	"github.com/minio/minio-go/v7"
	"github.com/minio/minio-go/v7/pkg/credentials"
)

// BlobsConfig moves the content of very large spans to S3-compatible storage (DESIGN.md §4, §7):
// the staged document keeps a searchable prefix of the text and `blob_uri`; the full content is
// read back from the blob by the evaluation worker.
type BlobsConfig struct {
	// S3 endpoint, e.g. https://s3.eu-west-1.amazonaws.com or http://minio:9000. Empty: disabled.
	Endpoint       string `mapstructure:"endpoint"`
	Bucket         string `mapstructure:"bucket"`
	Prefix         string `mapstructure:"prefix"`
	Region         string `mapstructure:"region"`
	AccessKey      string `mapstructure:"access_key"`
	SecretKey      string `mapstructure:"secret_key"`
	ThresholdBytes int    `mapstructure:"threshold_bytes"`
}

type blobStore struct {
	cfg    BlobsConfig
	client *minio.Client
}

func newBlobStore(cfg BlobsConfig) (*blobStore, error) {
	u, err := url.Parse(cfg.Endpoint)
	if err != nil || u.Host == "" {
		return nil, fmt.Errorf("blobs.endpoint %q: want scheme://host[:port]", cfg.Endpoint)
	}
	creds := credentials.NewStaticV4(cfg.AccessKey, cfg.SecretKey, "")
	if cfg.AccessKey == "" {
		creds = credentials.NewChainCredentials([]credentials.Provider{&credentials.EnvAWS{}, &credentials.IAM{}})
	}
	client, err := minio.New(u.Host, &minio.Options{Creds: creds, Secure: u.Scheme == "https", Region: cfg.Region,
		BucketLookup: minio.BucketLookupPath})
	if err != nil {
		return nil, err
	}
	return &blobStore{cfg: cfg, client: client}, nil
}

func contentSize(content map[string]any) int {
	n := 0
	for _, v := range content {
		if s, ok := v.(string); ok {
			n += len(s)
		} else {
			b, _ := json.Marshal(v)
			n += len(b)
		}
	}
	return n
}

// put stores the content verbatim (as a JSON object of the original attribute values).
func (b *blobStore) put(ctx context.Context, tenant, traceID, spanID string, content map[string]any) (string, error) {
	body, err := json.Marshal(content)
	if err != nil {
		return "", err
	}
	key := strings.TrimLeft(b.cfg.Prefix+tenant+"/"+traceID+"/"+spanID+".json", "/")
	_, err = b.client.PutObject(ctx, b.cfg.Bucket, key, bytes.NewReader(body), int64(len(body)),
		minio.PutObjectOptions{ContentType: "application/json"})
	if err != nil {
		return "", err
	}
	return "s3://" + b.cfg.Bucket + "/" + key, nil
}
