package web

import (
	"context"
	"net"
	"net/http"

	"github.com/gorilla/handlers"
	log "github.com/sirupsen/logrus"
	"goauthentik.io/internal/config"
)

type allowedProxyRequestContext string

const allowedProxyRequest allowedProxyRequestContext = ""

func IsRequestFromTrustedProxy(r *http.Request) bool {
	return r.Context().Value(allowedProxyRequest) != nil
}

// ProxyHeaders Set proxy headers like X-Forwarded-For and such, but only if the direct connection
// comes from a client that's in a list of trusted CIDRs
func ProxyHeaders() func(http.Handler) http.Handler {
	nets := []*net.IPNet{}
	for _, rn := range config.Get().Listen.TrustedProxyCIDRs {
		_, cidr, err := net.ParseCIDR(rn)
		if err != nil {
			continue
		}
		nets = append(nets, cidr)
	}
	return func(h http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			host, _, err := net.SplitHostPort(r.RemoteAddr)
			if err == nil {
				// remoteAddr will be nil if the IP cannot be parsed
				remoteAddr := net.ParseIP(host)
				for _, allowedCidr := range nets {
					if remoteAddr != nil && allowedCidr.Contains(remoteAddr) {
						log.WithField("remoteAddr", remoteAddr).WithField("cidr", allowedCidr.String()).Trace("Setting proxy headers")
						rr := r.WithContext(context.WithValue(r.Context(), allowedProxyRequest, true))
						handlers.ProxyHeaders(h).ServeHTTP(w, rr)
						return
					}
				}
			}
			// Request is not directly coming from a CIDR we "trust"
			// so set XFF to the direct host IP
			r.Header.Set("X-Forwarded-For", host)
			h.ServeHTTP(w, r)
		})
	}
}
