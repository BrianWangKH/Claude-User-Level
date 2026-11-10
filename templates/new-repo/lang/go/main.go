// Command {{NAME}} is the skeleton the home new-repo kit creates; replace it at kickoff.
package main

import "fmt"

// Greeting is the text main prints.
func Greeting(name string) string {
	return fmt.Sprintf("hello, %s", name)
}

func main() {
	fmt.Println(Greeting("{{NAME}}"))
}
