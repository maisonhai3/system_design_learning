package main

import "testing"

func TestPrice(t *testing.T) {
	tests := []struct {
		name    string
		item    string
		qty     int
		want    int
		wantErr bool
	}{
		{"known item", "widget", 2, 4000, false},
		{"single", "doohickey", 1, 30000, false},
		{"unknown item is rejected at the edge", "sprocket", 1, 0, true},
		{"zero qty", "widget", 0, 0, true},
		{"negative qty", "widget", -3, 0, true},
		{"absurd qty", "widget", 101, 0, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := price(tt.item, tt.qty)
			if (err != nil) != tt.wantErr {
				t.Fatalf("price(%q,%d) error = %v, wantErr %v", tt.item, tt.qty, err, tt.wantErr)
			}
			if got != tt.want {
				t.Fatalf("price(%q,%d) = %d, want %d", tt.item, tt.qty, got, tt.want)
			}
		})
	}
}
