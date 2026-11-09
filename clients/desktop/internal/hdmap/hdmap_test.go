package hdmap

import (
	"encoding/json"
	"os"
	"reflect"
	"testing"
)

type vectorsDoc struct {
	Format  string `json:"format"`
	Version int    `json:"version"`
	Cases   []struct {
		Name     string    `json:"name"`
		Messages []Message `json:"messages"`
		Expect   []File    `json:"expect"`
	} `json:"cases"`
	OverlayCases []struct {
		Name     string    `json:"name"`
		Messages []Message `json:"messages"`
		Overlay  string    `json:"overlay"`
		Expect   []File    `json:"expect"`
	} `json:"overlay_cases"`
	MergeCases []struct {
		Name         string            `json:"name"`
		A            string            `json:"a"`
		B            string            `json:"b"`
		ExpectRev    int               `json:"expect_rev"`
		ExpectRename map[string]string `json:"expect_rename"`
	} `json:"merge_cases"`
}

func loadVectors(t *testing.T) vectorsDoc {
	t.Helper()
	path := "../../../spec/vectors.json"
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	var doc vectorsDoc
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("parse %s: %v", path, err)
	}
	if doc.Format != "haodrive-map-vectors" {
		t.Fatalf("unexpected format %q", doc.Format)
	}
	return doc
}

func TestRebuildVectors(t *testing.T) {
	doc := loadVectors(t)
	for _, testCase := range doc.Cases {
		testCase := testCase
		t.Run(testCase.Name, func(t *testing.T) {
			got := Rebuild(testCase.Messages)
			if !reflect.DeepEqual(got, testCase.Expect) {
				t.Fatalf("rebuild mismatch\n got: %+v\nwant: %+v", got, testCase.Expect)
			}
		})
	}
}

func TestOverlayVectors(t *testing.T) {
	doc := loadVectors(t)
	for _, testCase := range doc.OverlayCases {
		testCase := testCase
		t.Run(testCase.Name, func(t *testing.T) {
			overlay := ParseOverlay(testCase.Overlay)
			got := overlay.Apply(Rebuild(testCase.Messages))
			if !reflect.DeepEqual(got, testCase.Expect) {
				t.Fatalf("overlay mismatch\n got: %+v\nwant: %+v", got, testCase.Expect)
			}
		})
	}
}

func TestMergeVectors(t *testing.T) {
	doc := loadVectors(t)
	for _, testCase := range doc.MergeCases {
		testCase := testCase
		t.Run(testCase.Name, func(t *testing.T) {
			merged := Merge(ParseOverlay(testCase.A), ParseOverlay(testCase.B))
			if merged.Rev != testCase.ExpectRev {
				t.Fatalf("rev = %d, want %d", merged.Rev, testCase.ExpectRev)
			}
			for key, name := range testCase.ExpectRename {
				var id int64
				if err := json.Unmarshal([]byte(key), &id); err != nil {
					t.Fatalf("bad vector key %q", key)
				}
				if merged.Renames[id] != name {
					t.Fatalf("rename[%d] = %q, want %q", id, merged.Renames[id], name)
				}
			}
		})
	}
}

func TestMergeIsSymmetricOnHashTie(t *testing.T) {
	a := ParseOverlay("rev: 5\nupdated: 2026-11-09T10:00:00Z\nrename 0 1 a.txt\n")
	b := ParseOverlay("rev: 5\nupdated: 2026-11-09T10:00:00Z\nrename 0 1 b.txt\n")
	forward := Merge(a, b)
	backward := Merge(b, a)
	if !reflect.DeepEqual(forward, backward) {
		t.Fatalf("merge must be symmetric: %+v vs %+v", forward, backward)
	}
	if forward.Rev != 5 {
		t.Fatalf("rev = %d, want 5", forward.Rev)
	}
}

func TestParseCaption(t *testing.T) {
	valid := []struct {
		caption string
		index   int
		total   int
	}{
		{"[TD-PART 1/2]", 1, 2},
		{"[TD-PART 3/3]", 3, 3},
		{"  [TD-PART 1/2]  ", 1, 2},
		{"[TD-PART 12/340]", 12, 340},
	}
	for _, testCase := range valid {
		index, total, ok := ParseCaption(testCase.caption)
		if !ok || index != testCase.index || total != testCase.total {
			t.Fatalf("ParseCaption(%q) = %d,%d,%v", testCase.caption, index, total, ok)
		}
	}
	invalid := []string{
		"", "TD-PART 1/2", "[TD-PART 0/2]", "[TD-PART 3/2]", "[TD-PART 1/2] extra",
		"[td-part 1/2]", "[TD-PART a/b]", "[TD-PART 1/]", "[TD-PART /2]",
	}
	for _, caption := range invalid {
		if _, _, ok := ParseCaption(caption); ok {
			t.Fatalf("ParseCaption(%q) should fail", caption)
		}
	}
}

func TestPartName(t *testing.T) {
	if got := PartName("movie.mkv", 1, 1); got != "movie.mkv" {
		t.Fatalf("single part name = %q", got)
	}
	cases := map[int]string{1: "m.bin.part001", 9: "m.bin.part009", 10: "m.bin.part010", 999: "m.bin.part999"}
	for index, want := range cases {
		if got := PartName("m.bin", index, 1000); got != want {
			t.Fatalf("PartName(%d) = %q, want %q", index, got, want)
		}
	}
	if got := BaseName("m.bin.part042"); got != "m.bin" {
		t.Fatalf("BaseName = %q", got)
	}
	if got := BaseName("weird.part001.txt"); got != "weird.part001.txt" {
		t.Fatalf("BaseName must only strip a trailing suffix, got %q", got)
	}
}

func TestPlanParts(t *testing.T) {
	if got := PlanParts(0, 4); !reflect.DeepEqual(got, []int64{0}) {
		t.Fatalf("zero size = %v", got)
	}
	if got := PlanParts(10, 4); !reflect.DeepEqual(got, []int64{4, 4, 2}) {
		t.Fatalf("10/4 = %v", got)
	}
	if got := PlanParts(8, 4); !reflect.DeepEqual(got, []int64{4, 4}) {
		t.Fatalf("8/4 = %v", got)
	}
	if got := PlanParts(1, 4); !reflect.DeepEqual(got, []int64{1}) {
		t.Fatalf("1/4 = %v", got)
	}
}

func TestOverlayRoundTrip(t *testing.T) {
	original := ParseOverlay("rev: 9\nupdated: 2026-11-09T12:00:00Z\nrename -100 7 renamed name.bin\norder -100 7 2\n")
	serialized := original.String()
	reparsed := ParseOverlay(serialized)
	if reparsed.Rev != 9 || reparsed.Channel != -100 || reparsed.Updated.IsZero() {
		t.Fatalf("round trip lost header: %+v", reparsed)
	}
	if reparsed.Renames[7] != "renamed name.bin" || reparsed.Orders[7] != 2 {
		t.Fatalf("round trip lost body: %+v", reparsed)
	}
	if _, ok := reparsed.Renames[7]; !ok || len(reparsed.Renames) != 1 {
		t.Fatalf("unexpected renames: %+v", reparsed.Renames)
	}
}

func TestEmptyOverlayIsHarmless(t *testing.T) {
	overlay := ParseOverlay("")
	files := Rebuild([]Message{{ID: 1, FileName: "a.txt", Size: 1}})
	applied := overlay.Apply(files)
	if !reflect.DeepEqual(applied, files) {
		t.Fatalf("empty overlay changed files: %+v", applied)
	}
	if overlay.Rev != 0 {
		t.Fatalf("empty overlay rev = %d", overlay.Rev)
	}
}
