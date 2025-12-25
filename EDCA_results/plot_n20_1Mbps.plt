set terminal pngcairo size 800,600 enhanced font 'Verdana,10'
set output 'plot_n20_1Mbps.png'
set title '802.11n EDCA Performance (nSta=20, Rate=1Mbps)'
set style data histograms
set style fill solid 1.0 border -1
set ylabel 'Throughput (Mbps)'
set xlabel 'Access Category'
set yrange [0:*]
set grid y
plot 'plot_n20_1Mbps.dat' using 2:xtic(1) title 'Throughput' lc rgb '#3498db'
