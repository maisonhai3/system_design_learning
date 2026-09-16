package labhttp

import (
	"os"
	"strconv"
)

// Env reads a setting with a default. Every service in this lab is configured
// this way and by nothing else: no config file, no flags, no service discovery.
// One of the quieter benefits of small services is that this is enough.
func Env(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

func EnvInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return def
}
